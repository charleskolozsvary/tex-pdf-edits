import logging
logger = logging.getLogger(__name__)

import argparse
import pymupdf
import json
import time
import pickle
import re
import sys

import texpdfedits.extractanns as extractanns
from texpdfedits.extractanns import XrefObj
import texpdfedits.marktex as marktex
import texpdfedits.utils as utils

import functools
from icecream import ic
from dataclasses import dataclass
from pathlib import Path

VERTICAL_BOX_OVERLAP_PERCENTAGE = 0.5

"""
When a rectangle doesn't intersect any word boxes, we look for
the word boxes "before" and "after" the rectangle. If the given
rectangle has y0 Y and a word box has y0 Y+.01, it would by
default be recognized as coming "after" the given rectangle
when very often it could actually appear earlier in the line.

To address this, we introduce a threshold so a word box is "after"
(or "before") the given rectangle only if its y0 is greater than
the given y0 plus this buffer (or less than the given y0 minus
this buffer). This is used in determining boxes_before and boxes_after.

We may need to eventually find a new and better way to determine word
box order if we continue to encounter issues (but I haven't so far).
"""

def getPageLabels(pdf_file: Path) -> list[str]:
    # is it a likely possibility that some of the labels are empty and others aren't?
    doc = pymupdf.open(pdf_file)
    page0 = doc[0]
    if not page0.get_label():
        return [str(i+1) for i, page in enumerate(doc)]
        
    page_labels = [page.get_label() for page in doc]
    
    if '' in page_labels:
        logger.error(f"{pdf_file} has an empty page label...")
        
    # ic(page_labels)
    return page_labels

def sortBoxes(boxes):
    if not boxes:
        return []

    by_y = sorted(boxes, key=lambda b: b.y0)
    lines = [[by_y[0]]]

    for box in by_y[1:]:
        line = lines[-1]
        line_y0 = min(b.y0 for b in line)
        line_y1 = max(b.y1 for b in line)
        vertical_overlap = max(0, min(line_y1, box.y1) - max(line_y0, box.y0))
        # if the overlap is at least half (percentage) of the height of the smaller box,
        # the boxes are on the same line
        if (vertical_overlap >=
            VERTICAL_BOX_OVERLAP_PERCENTAGE * min(line_y1 - line_y0, box.y1 - box.y0)):
            line.append(box)
        else:
            lines.append([box])

    result = []
    for line in lines:
        result.extend(sorted(line, key=lambda b: b.x0))
    return result

def categorizeMarkIDs(mark_ids: list[str]) -> int:
    """
    If the marks are all the same level of nesting, e.g.,
    all boxes are within the same footnote, return 'compatible'
    
    If the marks are all the same level of nesting except for the head count,
    e.g., all boxes are within thanks but include boxes in two different thanks,
    return 'maybe compatible'
    
    If the marks are not the same at all, e.g., one is in just in the
    document and another is in a footnote (inside the document),
    return 'incompatible'
    """
    sets_of_counter_info = {}
    count_info_lens = set()
    for mark_id in mark_ids:
        count_info = marktex.markIdToCountInfo(mark_id)
        count_info_lens.add(len(count_info))
        for i, name_head_stem in enumerate(count_info):
            if i not in sets_of_counter_info:
                sets_of_counter_info[i] = {
                    'names': set(),
                    'heads': set(),
                    'stems': set(),
                }
            sets_of_counter_info[i]['names'].add(name_head_stem['name'])
            sets_of_counter_info[i]['heads'].add(name_head_stem['head'])
            sets_of_counter_info[i]['stems'].add(name_head_stem['stem'])
            
    if len(count_info_lens) != 1:
        return 'incompatible'

    max_counter_idx = max(sets_of_counter_info.keys())
    # walk through lengths piece by piece
    for index, set_of_c_info in sets_of_counter_info.items():
        if len(set_of_c_info['names']) != 1:
            return 'incompatible'
        if len(set_of_c_info['heads']) != 1:
            if index < max_counter_idx:
                return 'incompatible'
            else:
                return 'maybe compatible'
        if len(set_of_c_info['stems']) != 1 and index < max_counter_idx:
            return 'incompatible'
        
    return 'compatible'

def isSimpleID(mark_id: str) -> bool:
    names = [
        c['name']
        for c in marktex.markIdToCountInfo(mark_id)
    ]
    return names in (['DOCUMENT'], ['DOCUMENT', 'ABSTRACT'])

def getTerminalStem(mark_id: str) -> int:
    count_info = marktex.markIdToCountInfo(mark_id)
    return count_info[-1]['stem']

def infoToMarkID(count_info: list[dict[str, str]]):
    return ','.join(
        [
            f"{piece['name']}{piece['head']};{piece['stem']}"
            for piece in count_info
        ]
    )

def getAdjacentPageLabel(
        page_labels: list[str],
        page_label: str,
        plus_minus: int
) -> str | None:
    idx = page_labels.index(page_label)
    # allow value error if page_label not in page_labels
    adj_idx = idx + plus_minus
    if adj_idx < 0 or adj_idx >= len(page_labels):
        return None
    return page_labels[adj_idx]    

def getAdjacentKey(
        mark_id: str,
        plus_minus: int,
        page_label: str,
        tex_word_boxes: dict[int, dict[str, pymupdf.Rect]],
        page_labels: list[str],
) -> str:
    """return the previous and next key based on the terminal stem value.
    So document0;0,caption0;1,footnote5;10
    should return

    document0;0,caption0;1,footnote5;9
    and
    document0;0,caption0;1,footnote5;11
    """
    count_info = marktex.markIdToCountInfo(mark_id)
    stem_val = int(count_info[-1]['stem']) 
    count_info[-1]['stem'] = str(stem_val + plus_minus)
    adjacent_mark = infoToMarkID(count_info)

    adjacent_pagelabel = getAdjacentPageLabel(page_labels, page_label, plus_minus)
        
    if adjacent_mark in tex_word_boxes[page_label]:
        return adjacent_mark
    elif (
        adjacent_pagelabel in tex_word_boxes
        and adjacent_mark in tex_word_boxes[adjacent_pagelabel]
    ):
        return adjacent_mark
    else:
        return None

def checkMaybeCompatible(
        mark_ids: list[str],
        mark_positions: dict[str, tuple[int, int]],        
) -> tuple[str, str]:
    """mark_ids are "maybe compatible" if their counters other
    than the last are known to all be equal

    In word's, the intention of this function is to look
    at each group of counters which have the same head value
    and compare the last in that group to the first in the
    next group with the same head value.

    (Typically it will have head value of just one more, but we don't require this.)

    If the number of characters between the end and the start
    for each of these pairs is less than some arbitrary threshold,
    say 100 characters, Then we'll return the key of the first id
    in the lowest head count group and the key of the last id in
    the largest head count group as our start_ and end_ extraction keys.
    """
    count_infos = [
        marktex.markIdToCountInfo(m_id)
        for m_id in mark_ids
    ]
    head_partitions = {}
        
    for c_info in count_infos:
        # [-1] because we already know that all preceding 
        # count information is the same            
        head_count = c_info[-1]['head']
        if head_count in head_partitions:
            head_partitions[head_count].append(c_info)
        else:
            head_partitions[head_count] = [c_info]
    sorted_head_counts = list(sorted(head_partitions.keys()))

    def returnCinfoStem(c_info: list[dict[str, str | int]]):
        return c_info[-1]['stem']

    for i in range(len(sorted_head_counts)-1):
        curr_hcount = sorted_head_counts[i]
        next_hcount = sorted_head_counts[i+1]

        last_curr_hpartitions = max(head_partitions[curr_hcount], key=returnCinfoStem)
        # ic(last_curr_hpartitions)
        
        last_curr = infoToMarkID(last_curr_hpartitions)

        first_next_hpartitions = min(head_partitions[next_hcount], key=returnCinfoStem)
        # ic(first_next_hpartitions)
        
        first_next = infoToMarkID(first_next_hpartitions)

        start_pos = mark_positions[last_curr][1]
        end_pos = mark_positions[first_next][0]
        if not start_pos < end_pos:
            logger.debug(
                f"Intermediate start_ends '{start_pos}' '{end_pos}' were out of order"
            )
            return (None, None)    

    start_key = infoToMarkID(
        min(head_partitions[sorted_head_counts[0]], key=returnCinfoStem)
    )
        
    end_key = infoToMarkID(
        max(head_partitions[sorted_head_counts[-1]], key=returnCinfoStem)
    )
    return start_key, end_key

def getAdjacentPageID(
        tex_word_boxes: dict[int, dict[str, pymupdf.Rect]],
        page_label: str,
        first_or_last: str
) -> str:
    page_word_boxes = tex_word_boxes.get(page_label, None)
    
    if page_word_boxes is None:
        logger.debug(f"No tex_word_boxes on page {page_label}")
        return None

    simpleIDs = [
        mark_id
        for mark_id in page_word_boxes.keys()
        if isSimpleID(mark_id)
    ]
    
    if not simpleIDs:
        logger.warning(f"No simple IDs on page {page_label}")
        return None
    
    match first_or_last:
        case 'first':
            return min(simpleIDs, key = getTerminalStem)
        case 'last':
            return max(simpleIDs, key = getTerminalStem)
        case _:
            logger.error(
                f"first_or_last was {first_or_last}"
                ": not 'first' or 'last'"
            )
            return None

def compareBoxes(a, b):
    if abs(a.y0 - b.y0) < BOXES_Y_EQUIV_RANGE:
        if a.x0 < b.x0:
            return -1
        elif a.x0 > b.x0:
            return 1
        else:
            return 0
    else:
        if a.y0 < b.y0:
            return -1
        else:
            return 1

def getPositionalBoxesBeforeAfter(in_rectangle, page_word_boxes):
    just_boxes = [box for box in page_word_boxes.values()]
    just_boxes.append(in_rectangle)
    
    sorted_boxes = sortBoxes(just_boxes)
    inrect_idx = sorted_boxes.index(in_rectangle)
    
    boxes_before = sorted_boxes[:inrect_idx]
    boxes_after  = sorted_boxes[inrect_idx+1:]
    return (boxes_before, boxes_after)
    

def useAllIDs(in_rectangle, page_word_boxes, tex_word_boxes, page_label, page_labels):
    """
    Try to find the before and after boxes by looking at nearby boxes of any kind (not just simple)
    """
    (boxes_before, boxes_after) = getPositionalBoxesBeforeAfter(in_rectangle, page_word_boxes)

    boxes_to_ids = {
        box : id
        for id, box in page_word_boxes.items()
    }

    prev_pagelabel = getAdjacentPageLabel(page_labels, page_label, -1)
    post_pagelabel = getAdjacentPageLabel(page_labels, page_label,  1)

    if not boxes_before:
        start_key = getAdjacentPageID(tex_word_boxes, prev_pagelabel, 'last')
    else:
        start_key = boxes_to_ids[boxes_before[-1]]

    if not boxes_after:
        end_key = getAdjacentPageID(tex_word_boxes, post_pagelabel, 'first')
    else:
        end_key = boxes_to_ids[boxes_after[0]]

    if start_key is None or end_key is None:
        return (start_key, end_key)

    category = categorizeMarkIDs([start_key, end_key])

    if category != 'compatible':
        logger.debug(
            f"Start and end IDs ('{start_key}', '{end_key}') "
            "from were not compatible (in no-intersection case)"
        )        
        return (None, None)

    return (start_key, end_key)

def useSimpleIDs(in_rectangle, page_word_boxes, tex_word_boxes, page_label, page_labels):
    """
    Try to find the before and after boxes by just looking at nearby boxes with simple IDs
    """
    (all_before, all_after) = getPositionalBoxesBeforeAfter(in_rectangle, page_word_boxes)
    boxes_before = {
        k: rect
        for k, rect in page_word_boxes.items()
        if rect in all_before
        and isSimpleID(k)
    }
    boxes_after = {
        k: rect
        for k, rect in page_word_boxes.items()
        if rect in all_after
        and isSimpleID(k)
    }

    # logger.debug(f"boxes before: {boxes_before}\n\n")
    # logger.debug(f"boxes after: {boxes_after}\n\n")        
        
    start_key = max(boxes_before.keys(), key=getTerminalStem) if boxes_before else None
    end_key = min(boxes_after.keys(), key=getTerminalStem) if boxes_after else None

    behind = -1
    prev_pagelabel = getAdjacentPageLabel(page_labels, page_label, behind)
    while prev_pagelabel not in tex_word_boxes and prev_pagelabel in page_labels:
        behind -= 1
        prev_pagelabel = getAdjacentPageLabel(page_labels, page_label, behind)

    ahead = 1
    post_pagelabel = getAdjacentPageLabel(page_labels, page_label,  ahead)
    while post_pagelabel not in tex_word_boxes and post_pagelabel in page_labels:
        ahead += 1
        post_pagelabel = getAdjacentPageLabel(page_labels, page_label, ahead)
        
    if start_key is None:
        simple_IDs = [fid for fid in tex_word_boxes[prev_pagelabel] if isSimpleID(fid)] if prev_pagelabel in tex_word_boxes else []
        start_key = max(simple_IDs, key=getTerminalStem) if len(simple_IDs) > 0 else None

    if end_key is None:
        simple_IDs = [fid for fid in tex_word_boxes[post_pagelabel] if isSimpleID(fid)] if post_pagelabel in tex_word_boxes else []
        end_key = min(simple_IDs, key=getTerminalStem) if len(simple_IDs) > 0  else None

    return (start_key, end_key)
                
def rectangleToLatex(
        page_labels: list[str],
        page_label: str,
        in_rectangle: pymupdf.Rect,
        tex_word_boxes: dict[int, dict[str, pymupdf.Rect]],
        mark_positions: dict[str, tuple[int, int]],
        tex_str: str
) -> tuple[str, tuple[int, int]] | tuple[None, None]:
    r"""
    Args:
        page_label: marked page label 
        in_rectangle: Rectangle on the page (pymupdf format)
        tex_word_boxes: Dictionary from getWordBoxes()
        in marktex.py mark_positions:
        dictionary mapping mark_id -> (start, end) positions in tex_str 
        tex_str: original unmarked LaTeX source

    Returns: The (unmarked) source LaTeX snippet which
    "contains" the rectangle.

    Logic:
    If the inputted rectangle intersects at least one
    word box, then we consider three possibilities
         1. The word boxes are "compatible" -> we use the
            boxes within that level: ids are first preceding,
            next following
    
         2. The word boxes are "maybe compatible" -> we have
            partitions of ids by head value, and we check the pairs
            (last stem of head i, first stem of head i + 1)
            and see if their distance in source position (in
            characters) is more than some threshold.
    
            If all of these distances are less than a threshold, then
            we give all the source between the box before the
            earliest intersected head and the box after the
            last intersected head.

            If the distances are not all within that threshold
            then we don't extract the source
    
          3. The word boxes are "incompatible"
             -> we don't extract any source

    Otherwise, (the inputted rectangle does not intersect any boxes) ->
    we order the boxes including the inputted one by x then by y and
    take the start key to be the last of the ones before and the end
    key to be the first of the ones after (we also check compatibility)

    And we do some additional handling if there are no boxes
    before or after on that page
    """
    if page_label not in tex_word_boxes:
        logger.warning(
            f"Cannot extract LaTeX: "
            f"page {page_label} not in tex_word_boxes"
        )
        return (None, None)

    page_word_boxes = tex_word_boxes[page_label]
    intersecting_word_boxes = {
        k : rect
        for k, rect in page_word_boxes.items()
        if in_rectangle.intersects(rect)
    }

    if intersecting_word_boxes:
        # logger.debug(f"Rectangle {in_rectangle} on page {page_label} intersected {len(intersecting_word_boxes)} word boxes")
        mark_ids = list(intersecting_word_boxes.keys())
        category = categorizeMarkIDs(mark_ids)
        if category == 'compatible':
            min_key = min(mark_ids, key=getTerminalStem)
            max_key = max(mark_ids, key=getTerminalStem)
            
            before_min = getAdjacentKey(
                min_key,
                -1,
                page_label,
                tex_word_boxes,
                page_labels,
            )
            after_max = getAdjacentKey(
                max_key,
                1,
                page_label,
                tex_word_boxes,
                page_labels,
            )
            
            start_key = before_min if before_min is not None else min_key
            end_key = after_max if after_max is not None else max_key
        elif category == 'maybe compatible':
            (start_key, end_key) = checkMaybeCompatible(mark_ids, mark_positions)
        else:
            logger.warning(
                f"Cannot extract LaTeX: "
                f"intersected mark IDs {mark_ids} "
                "were not compatible"
            )
            logger.debug(f"Incompatible mark IDs were\n{mark_ids}")
            return (None, None)
    else:
        logger.debug(
            f"Rectangle {in_rectangle} did NOT intersect "
            f"any word box on page {page_label}"
        )
        
        (start_key, end_key) = useAllIDs(in_rectangle, page_word_boxes, tex_word_boxes, page_label, page_labels)

        if start_key is None or end_key is None:
            (start_key, end_key) = useSimpleIDs(in_rectangle, page_word_boxes, tex_word_boxes, page_label, page_labels)

    if start_key is None or end_key is None:
        # This should only happen if
        # (1) the rectangle doesn't intersect any boxes and it comes before or after all of them
        # (2) the rectangle intersects boxes which have incompatible ids
        # (2.1) the rectangle intersects boxes which are maybe compatible that are actually deemed incompatible by checkMaybeCompatible
        logger.warning(
            f"Cannot extract LaTeX: "
            f"Rectangle outside marked boxes "
            f"(start_key={start_key}, end_key={end_key})"
        )
        return (None, None)

    # logger.debug(f"Before key is {start_key} and after key is {end_key}")

    start_pos = mark_positions[start_key][0]
    end_pos = mark_positions[end_key][1]

    if start_pos > end_pos:
        # this shouldn't happen thanks to BOXES_Y_EQUIV_RANGE
        logger.warning(
            f"Cannot extract LaTeX: "
            f"start_pos = '{start_pos}' > '{end_pos}' = end_pos"
        )
        return (None, None)
    
    return (tex_str[start_pos:end_pos], (start_pos, end_pos))

def toCodeblock(string: str, language: str = 'latex'):
        return f"```{language}\n{string}\n```"

def markdownReplies(replies: list[str]):
    if not replies:
        return ''
    output = '\n\n### Replies '
    for i in range(len(replies)):
        output += (
            f'\n\n#### Reply {i+1}\n'
            f'```text\n{replies[i]}\n```'
        )
    return output

@dataclass
class NestedCounter:
    value: int
    total: int
    
class Correction:
    """
    Includes all the information I need to produce and
    debug the individual correction prompts. There's
    probably room for improvement in the terminology,
    but an "edit" is the information I get from a PDF
    annotation; and a "correction" is that information
    combined with the corresponding latex_snippet
    which is needed to carry out the "edit".
    
    Attributes:
    index: the zero-indexed correction number
    
    pageno: the (also zero-indexed) page the correction appears on (absolute page number from annotated PDF)

    page_label: the written page number the correction appears on (from PDF generated by source)
    
    type: the annotation type of the correction, e.g.,
          "Caret", "Strikeout", "Highlight".
    
          These are a tuple where the first value is an int as listed
          at https://pymupdf.readthedocs.io/en/latest/vars.html#annotationtypes
          (see PDF_ANNOT_TEXT for example) and the second is a string
          which is a name pymupdf supplies but isn't easily accessible
          other than through an actual annotation object which was
          processed in extractanns.py
    
    messages: the text written in the annotation comment box and
          any replies to it (which are sorted by date)
    
    pdf_selected_text: the text extracted from the PDF.
          HTML-like focus tags indicate which text was
          selected by the annotaiton.

    pdf_annot_rect: the rectangle of the correction annotation from
          the that page of the PDF

    **pdf_selection_bbs: the rectangles used to partition the text
          extracted from the pdf_annot_rect into
          pieces which are and are not inside the HTML-like
          focus tags. See getSelection in extract.py for more on this
    
    latex_snippet: the latex source which (more or less) rendered
          the pdf_selected_text. See marktex.py for more on how this
          was retrieved
    
    snippet_source_positions: the start and end positions of the latex_snippet
          in the original latex_string.

          The latex_snippet is simply tex_str[start:end] where tex_str
          is the source LaTeX as a string
    """
    def __init__(
            self,
            index: int,
            nested_count: NestedCounter,
            pageno: int,
            page_label: str,
            type: tuple[int, str],
            xref: int,
            checkmark: XrefObj,
            status: XrefObj,
            messages: dict[str, str | list[str]],
            pdf_selected_text: str,
            pdf_annot_rect: pymupdf.Rect,
            pdf_selection_bbs: list[pymupdf.Rect],
            latex_snippet: str,
            snippet_source_positions: tuple[int, int],
    ) -> None:
        self.index                    = index
        self.nested_count             = nested_count
        self.pageno                   = pageno
        self.page_label               = page_label
        self.type                     = type
        self.checkmark                = checkmark
        self.status                   = status
        self.xref                     = xref        
        self.messages                 = messages
        self.pdf_selected_text        = pdf_selected_text
        self.pdf_annot_rect           = pdf_annot_rect
        self.pdf_selection_bbs        = pdf_selection_bbs
        self.latex_snippet            = latex_snippet
        self.snippet_source_positions = snippet_source_positions

        self.is_autocorrected = False
        self.group = None

    def __str__ (self): 
        return json.dumps({
            "index" : self.index,
            "nested_count": self.nested_count,
            "xref" : self.xref,
            "checkmark" : str(self.checkmark),
            "status" : str(self.status),
            "pageno": self.pageno,
            "page_label": self.page_label,
            "type": self.type[1],
            "messages": {
                "comment": self.messages['comment'],
                "responses": self.messages['responses']
            },
            "PDF selected text": self.pdf_selected_text,
            "PDF selection line rectangle": str(self.pdf_annot_rect),
            "LaTeX snippet": self.latex_snippet,
            "Snippet source positions": self.snippet_source_positions
        }, indent=4, ensure_ascii=False)

    def __repr__ (self):
        return str(self)

    def asCommentStart(self) -> str:
        import texpdfedits.formatcomm as formatcomm        
        replies = '", "'.join(
            utils.sanitize_pdf_text(reply)
            for reply in self.messages['responses']
        )
        return formatcomm.startComment(self, replies)
        
    def asCommentEnd(self) -> str:
        import texpdfedits.formatcomm as formatcomm
        replies = '", "'.join(
            utils.sanitize_pdf_text(reply)
            for reply in self.messages['responses']
        )
        return formatcomm.endComment(self, replies)
    
    def asMarkdownPrompt(self) -> str:
        replies = markdownReplies(self.messages['responses'])
        return rf"""### Annotation: {self.type[1]}

### Comment
```text
{self.messages['comment']}
```{replies}

### PDF selected text
```text
{self.pdf_selected_text}
```
  
### LaTeX snippet
```latex
{self.latex_snippet}
```"""
    
    def updateSnippet(
            self,
            new_source_pos: tuple[int, int],
            new_snippet: str
    ) -> None:
        self.snippet_source_positions = new_source_pos
        self.latex_snippet = new_snippet

    def snippetToCodeblock(self):
        return f"```latex\n{self.latex_snippet}\n```"


def groupOverlaps(
        keyed_start_ends: dict[int, tuple[int]]
) -> list[list[int]]:
    """
    I have a list of dictionaries where each dictionary has
    keys whose values are tuples with start and end values.
    
    I  group together all keys whose start and end values overlap.
    
    If there are no such keys, then I return an empty list.
    """
    if not keyed_start_ends:
        return []
    
    # sort by starts    
    keys = list(
        sorted(
            [k for k in keyed_start_ends],
            key = lambda k: keyed_start_ends[k][0]
        )
    ) 

    groups = []
    current_group = [keys[0]]
    curr_group_end = keyed_start_ends[keys[0]][1]
    for i, k in enumerate(keys):
        if i == 0:
            continue
        start = keyed_start_ends[k][0]
        end   = keyed_start_ends[k][1]
        if start <= curr_group_end:
            current_group.append(k)
            curr_group_end = max(curr_group_end, end)
        else:
            if len(current_group) >= 2:
                groups.append(current_group)
            current_group = [k]
            curr_group_end = end
    if len(current_group) >= 2:
        groups.append(current_group)
    return groups

def merge_overlapping_corrections(
        corrections: list[Correction],
        tex_str: str,
) -> tuple[list[list[int]], list[str]]:
    """
    Find which corrections overlap, and update the
    correction snippets (and source positions) to
    span the union of the overlapping corrections
    """
    
    if not corrections:
        return [], []

    key_to_correction = {corr.index: corr for corr in corrections}
    keyed_start_ends = {
        corr.index: corr.snippet_source_positions for corr in corrections
    }
    groups = groupOverlaps(keyed_start_ends)

    for group in groups:
        spans_in_group = [keyed_start_ends[k] for k in group]
        min_start = min(spans_in_group, key = lambda span: span[0])[0]
        max_end = max(spans_in_group, key = lambda span: span[1])[1]
        containing_snippet = tex_str[min_start:max_end]
        for k in group:
            corr = key_to_correction[k]
            if not corr.latex_snippet in containing_snippet:
                err_message = (
                     "Failed to create overlapping groups: "
                    f"a snippet \n{corr.snippetToCodeblock()}\n was not in its"
                    f" spanning snippet \n{toCodeblock(containing_snippet)}\n"
                )
                logger.critical(err_message)
                raise RuntimeError(err_message)
            corr.updateSnippet((min_start, max_end), containing_snippet)
            corr.group = group
    
    return groups

def apply_source_offset(
        source_offset: str,
        tex_word_boxes: dict[str, dict[str, pymupdf.Rect]],
):
    """
    assumes that the offset is not a roman numeral
    
    This whole thing is quite jank if the page label metadata isn't right
    and the PDF generated by the LaTeX and annotated PDF don't already 
    correspond one to one
    """
    def labelVal(pagelabel: str): 
        if re.search(r'^[ivxlcdm]+$', pagelabel, flags = re.IGNORECASE) is not None:
            # arbitrary negative offset so 
            val = -100 + utils.fromRoman(pagelabel)
        elif not pagelabel:
            val = 0
        else:
            val = int(pagelabel)
        return val

    remove = [
        label for label in tex_word_boxes
        if labelVal(label) < labelVal(source_offset)
    ]

    for rem in remove:
        del tex_word_boxes[rem]

    return {
        str(labelVal(label) - labelVal(source_offset) + 1) : info
        for label, info in tex_word_boxes.items()
    }

def write_vercorr_data(
        pdf_file: Path,
        corrections: list[Correction],
):
    script_name = Path(sys.argv[0]).name
    data_file = utils.replace_suffix(pdf_file, script_name)
    # only two fields right now, but if I wanted to extend this for some
    # reason later, json makes that simple enough
    data = '\n'.join(
        json.dumps({ 
            'index' : corr.index,
            'xref'  : corr.xref,
        })
        for corr in corrections
    )
    logger.info(f"Writing {data_file}...")
    utils.writeStringToFile(data, data_file)
    logger.info("Done")

def get_pageno_to_counters(edits: list[Edit]) -> dict[int, dict[int | str, int]]:
    result = dict()
    for edit in edits:
        pageno = edit.pageno
        if pageno not in result:
            result[pageno] = {
                edit.xref : 1,
                "total"   : 1,
            }
        else:
            result[pageno]["total"] += 1
            result[pageno][edit.xref] = result[pageno]["total"]
    return result

def getCorrections(
        pdf_file: Path,
        latex_file: Path,
        **opt
) -> tuple[list[Correction], list, int, int]:
    
    edits, n_annots = extractanns.getEdits(pdf_file, **opt)
    mark_positions, tex_word_boxes, begin_document_start = marktex.getSyncInfo(
        latex_file,
        **opt
    )

    n_edits = len(edits)

    if opt['tex_start']:
        logger.info(f"Treating {latex_file} start as page '{opt['tex_start']}'...")
        tex_word_boxes = apply_source_offset(opt['tex_start'], tex_word_boxes)
        logger.info("Done")
    
    tex_str = utils.sourceAsString(Path(latex_file))
    
    fallback_source_positions = (begin_document_start - 1, begin_document_start)
    fallback_latex_snippet = tex_str[begin_document_start - 1 : begin_document_start]
    num_could_not_locate = 0

    pageno_to_counters = get_pageno_to_counters(edits)

    logger.info("Making correction objects...")
    page_labels = getPageLabels(pdf_file)
    corrections = []
    for i, edit in enumerate(edits, start = 1):
        progress = f"{i}/{len(edits)-1}"
        pageno = edit.pageno
        page_label = edit.page_label
        pdf_annot_rect = edit.annot_rect
        
        if page_label not in tex_word_boxes:
            logger.warning(
                f"Could not locate correction {progress}: "
                f"page '{page_label}' not in tex_word_boxes for edit {edit}"
            )
            latex_snippet = fallback_latex_snippet
            snippet_source_positions = fallback_source_positions
            num_could_not_locate += 1
        else:
            logger.debug(f"Getting latex snippet for edit {edit}...")
            latex_snippet, snippet_source_positions = rectangleToLatex(
                page_labels,
                page_label,
                pdf_annot_rect,
                tex_word_boxes,
                mark_positions,
                tex_str
            )
            logger.debug(f"Done")
            if latex_snippet is None:
                logger.warning(
                    f"Could not locate correction {progress}: "
                    f"no LaTeX snippet for {edit}"
                )
                num_could_not_locate += 1
                latex_snippet = fallback_latex_snippet
                snippet_source_positions = fallback_source_positions

        corrections.append(
            Correction(
                i,
                NestedCounter(pageno_to_counters[pageno][edit.xref], pageno_to_counters[pageno]["total"]),
                pageno,
                page_label,
                edit.type,
                edit.xref,
                edit.checkmark,
                edit.status,
                edit.message,
                edit.selection,
                pdf_annot_rect,
                edit.selection_bbs,
                latex_snippet,
                snippet_source_positions,
            )
        )
    logger.info("Done")

    logger.info(
        f"Produced {len(corrections)} corrections from "
        f"{len(edits)} edit annotations"
    )

    overlapping_keys = []
    if opt['merge_overlapping']:
        overlapping_keys = merge_overlapping_corrections(
            corrections,
            tex_str,
        )
        logger.info("Overlapping corrections merged")
    else:
        logger.info("Overlapping corrections NOT merged")

    write_vercorr_data(pdf_file, corrections)

    return corrections, overlapping_keys, n_annots, n_edits, num_could_not_locate
