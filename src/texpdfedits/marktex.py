r"""
This module marks the source with
\markbox commands which record box and pdf
positioning information which is used to take any
(it is hoped) rectangle produce corresponding
source which rendered the text
which intersects said rectangle.

The function rectangleToLatex is implemented
in corr.py where the correction objects are created
"""

import argparse
import logging
logger = logging.getLogger(__name__)

from pylatexenc.latexwalker import (
    LatexWalker,
    LatexNode,
    LatexMacroNode,
    LatexEnvironmentNode,
    LatexGroupNode,
    LatexMathNode,
    LatexCharsNode,
    LatexCommentNode
)
from pylatexenc.macrospec import (
    LatexContextDb,
    std_macro,
    std_environment,
    MacroSpec,
    ParsedMacroArgs,
    EnvironmentSpec
)

from pathlib import Path
import re
import pymupdf
import time
import textwrap

import texpdfedits.utils as utils

MODULE_NAME = __spec__.name.split('.')[-1]

class IgnoreRegionArgsParser:
    def parse_args(self, w, pos, parsing_state, **kwargs):
        r"""
        Skip until \endignorepylatexenc using get_token
        use in rare circumstances where the pylatexenc
        parser fails on something (typically in the preamble)
        just add
        \def\startignorepylatexenc{}
        \def\endignorepylatexenc{}

        and then
        \startignorepylatexenc
        ...
        \endignorepylatexenc

        around any problematic code before running corrinline
        """
        current_pos = pos
        while True:
            try:
                # get_token returns (token_object, new_pos)
                tok = w.get_token(
                    current_pos,
                    parsing_state=parsing_state
                )
            except:
                raise ValueError(
                    "No matching \\endignorepylatexenc found"
                )
            
            if (tok.tok == 'macro'
                and tok.arg == 'endignorepylatexenc'):
                end_pos = tok.pos + tok.len
                break
            
            current_pos = tok.pos + tok.len
        
        return ParsedMacroArgs(argnlist=[]), end_pos, 0

# chosen csname for marking command
MARK_CSNAME = r'\markbox'

CSNAMES_ARGSPEC = {
    'emph': '{',
    'textup': '{',
    'textit': '{',
    'textbf': '{',
    'textsc': '{',
    'texttt': '{',
    'url': '{',
    'underline': '{',
    'part': '*[{',
    'chapter' : '*[{',
    'section': '*[{',
    'subsection': '*[{',
    'subsubsection': '*[{',
    'thanks': '{',
    'subjclass': '[{',
    'datereceived': '{',
    'daterevised': '{',
    'commby': '{',
    'dedicatory' : '{',
    'title': '[{',
    'author': '[{',
    'address': '[{',
    'curraddr' : '[{',
    'urladdr' : '[{',
    'email': '[{',
    'keywords': '{',
    'footnote': '[{',
    # \footnote[10]{text} sets the footnote to be footnote 10
    # only a number can be passed, nothing else
    'footnotemark': '[',
    'footnotetext': '[{',
    'caption': '[{',
    'item': '[',
    'renewcommand': '*{[{',
    'newcommand': '*{[{',
    'theoremstyle' : '{',
    'bibitem': '[{',
    'bib': '{{{',
    'newtheorem': '*{[{[', 
    'newenvironment': '{[[{{',
    'cite': '[{',
    'label': '[{',
    'copyrightinfo': '{{',
    'translator': '{',
    'vspace': '*{',
    'hspace': '*{',
    'documentclass': '[{',
    MARK_CSNAME: '{{',  # although I don't ever parse the marked latex
}

ENVNAMES_ARGSPEC = {
    'enumerate': '[',
    'itemize': '[',
    'description': '[',
}

# macros which get marked as
# \markbox{<key>}{\macro{...}}, like in-line math
# LOW PRIORITY: can I mark the words in these macros? 
MARKED_ENTIRE_CSNAMES = {
    'emph',
    'textit',
    'textbf',
    'textsc',
    'underline',
    'textup',
}
# any of the above fail if their arguments
# are abnormal, like a \[...\] in a \textit

# Below are macros whose contents ARE marked,
# not just the entire macro like the ones above
DISTINCTLY_MARKED_MACROS = {
    'caption': 1,
    'footnote': 1,
    'footnotetext': 1,
    'title': 1,
    'address': 1,
    'curraddr': 1,
    'urladdr': 1,
    'email': 1,
    'thanks': 0,
    'keywords': 0,
    'subjclass': 1,
    'dedicatory': 0,
    'translator': 0,
    'commby': 0,
    'copyrightinfo': 1,
    'bib': 2,
}

# environments which get their own nested numbering
DISTINCTLY_MARKED_ENVIRONMENTS = {'document', 'abstract'}

# the environments whose latex
# character nodes can be marked
ALLOWED_MARK_ENVIRONMENTS = {
    'document',
    'abstract',
    'proof',
    'enumerate',
    'itemize',
    'description',
    'thebibliography',
    'biblist',
    'bibdiv',
    'bibsection',
    'appendix',
    'allowdisplaybreaks',
    'center',
    'bookreview',
    'review',
    'appendices',
}

# amsrefs bib keys whose values cannot be marked
FORBIDDEN_BIB_KEYS = [
    'author',
    'language',
    'label',
    'date',
    'year',
    'url',
    'arXiv',
    'eprint',
    'edition',
    'editor',
]

# environments which are not marked in full like
# allowed_mark_environments---here only the contents
# of the \caption macros in said environments can be marked
ONLY_MARK_CAPTION_ENVS = {
    'table',
    'table*',
    'figure',
    'figure*',
    'longtable',
    'longtable*',
    'subfigure',
    'subfigure*'
}

ACCENT_MACROS = {
    '`', # grave
    "'", # acute
    '^', # circumflex
    '"', # umlaut, trema, or dieresis
    'H', # long Hungarian umlaut
    '~', # tilde
    'c', # cedilla
    'k', # ogonek
    '=', # macron
    'b', # bar under 
    '.', # dot over 
    'd', # dot under 
    'r', # ring over
    'u', # breve over
    'v', # caron over
    't', # tie over next two
}

"""
There are 72.27 TeX points in an inch, while there are 
72 PDF points (what TeX calls a big point or "bp") in an inch, which is what
pymupdf and other modern pdf systems use, so we need that conversion ratio
"""
TEX_POINTS_TO_PDF_POINTS_CONVERSION_RATIO = 72 / 72.27

# PdfTeX outputs the PDF positions in scaled points, so this constant is useful
SCALED_POINTS_PER_TEX_POINT = 2 ** 16 # 65536

def joinNodesVerbatim(nodelist: list[LatexNode]) -> str:
    return ''.join(
        node.latex_verbatim()
        for node in nodelist
    )

def getEnunciations(preamble_nodes) -> tuple[list[str], str]:
    r"""
    Retrieve \newtheorem declarations so we can
    expand ALLOWED_MARK_ENVIRONMENTS to include
    those enunciations, too.
    """
    enunciations = []
    for i, node in enumerate(preamble_nodes):
        if (node.isNodeType(LatexMacroNode)
            and node.macroname == 'newtheorem'):
            node_args = node.nodeargd.argnlist
            len_arg_spec = len(CSNAMES_ARGSPEC[node.macroname])
            if len(node_args) == len_arg_spec and len_arg_spec > 1:
                arg_one = node_args[1]
                try:
                    # this fails with \\newtheorem{%\nthm}{Theorem},
                    # but that's okay
                    enun_name = arg_one.nodelist[0].chars
                except Exception as e: 
                    logger.warning(
                        f"Attempting to extract second argument of "
                        f"'{node.latex_verbatim()}' "
                        f"raised {type(e).__name__}: {e}; "
                        f"node.nodeargs[1] was {arg_one}; ignoring"
                    )
                    continue
                enunciations.append({
                    'name': enun_name,
                    'start end': (node.pos, node.pos+node.len),
                    'verbatim': node.latex_verbatim()
                })
            else:
                logger.warning(
                    f"Malformed {node.macroname} "
                    f"'{node.latex_verbatim()}' "
                    f"did not match it's argument specification: "
                    f"{CSNAMES_ARGSPEC[node.macroname]}"
                )
    if not enunciations:
        logger.warning("No newtheorem commands found")
        
    return enunciations

def markNodes(
        to_mark_nodelist: list[LatexNode],
        allowed_environments: set[str],
        chars_node_match_regex: str,
) -> tuple[str, dict[str, dict[str, int]]]:
    """
    Recursively mark the passsed node list.
    Return the marked string and the mark counters
    """
    counters = {}

    # macro names where it is acceptable to match the
    # beggining of the string when marking a chars
    # node after it
    OTHER_PREV_MACRO_NODE_EXCEPTIONS = {' ', 'item'}
    # control space or item        
    
    def markStr(string: str, parent_counter_keys: list[str]) -> str:
        """
        A mark like document0:15;footnote0:3 corresponds to the fourth
        markbox in the first footnote which appears after
        the 16th markbox in the first (and only) document environment.
        So all numbers are zero indexed. The number after the colon
        corresponds to some box counter, and the number before the colon
        corresponds to the environment/macro counter

        So the general structure is
        <environment/macro><num>:<num>;<nested enuncation/macro>:<num>;<and so on>

        More than two levels of nesting should be very rare,
        but I use this format to handle arbitrary nesting

        parent_counter_keys are the names of environments/macros
        the current mark is to be written in.
        increment_head tells me whether I need to increment the
        head counter to the currently deepest nested object
        
        The very first time a particular dedicated macro/env is
        encountered (to mark in), increment_head should not be True, 
        the counter key is automatically initialized
        (and so there's no head value to reset)
        """
        if not parent_counter_keys:
            err_message = (
                f"Could not mark {string}: it is not "
                "within any recognized macro/environment"
            )
            logger.critical(err_message)
            raise RuntimeError(err_message)
            
        for parent in parent_counter_keys:
            if parent not in counters:
                counters[parent] = {'head': 0, 'value': -1}

        if parent_counter_keys[-1] == 'bib':
            cheated_for_bib = parent_counter_keys.pop()
        else:
            cheated_for_bib = ''
            
        count_key = parent_counter_keys[-1]
        counters[count_key]['value'] += 1
        
        mark_id = ','.join(
            f"{key.upper()}"
            f"{counters[key]['head']};"
            f"{counters[key]['value']}"
            for key in parent_counter_keys
        )

        if cheated_for_bib:
            parent_counter_keys.append('bib')
        
        return rf'{MARK_CSNAME}{{{mark_id}}}{{{string}}}'
        
    def recMark(node, parent_node, prev_node, parent_counter_keys):
        
        node_verbatim = node.latex_verbatim()
        
        if node.isNodeType(LatexEnvironmentNode):
            node_name = node.envname
            if (node_name in DISTINCTLY_MARKED_ENVIRONMENTS
                and node_name in counters):
                counters[node_name]['head'] += 1
                counters[node_name]['value'] = -1                    
        elif node.isNodeType(LatexMacroNode):
            node_name = node.macroname
            if (node_name in DISTINCTLY_MARKED_MACROS
                and node_name in counters):
                counters[node_name]['head'] += 1
                counters[node_name]['value'] = -1

        parent_is_distinctly_marked_macro = (
            parent_node is not None
            and parent_node.isNodeType(LatexMacroNode)
            and parent_node.macroname in DISTINCTLY_MARKED_MACROS
        )
        is_in_only_mark_caption_env = (
            parent_node is not None
            and parent_node.isNodeType(LatexEnvironmentNode)
            and parent_node.envname in ONLY_MARK_CAPTION_ENVS
        )
        
        in_preamble = 'document' not in parent_counter_keys
        in_bib = parent_counter_keys and 'bib' in parent_counter_keys
        
        if isinstance(node, LatexEnvironmentNode):
            env_args = node.nodeargd.argnlist
            verbatim_args = joinNodesVerbatim(env_args) if env_args != [None] else ''

            # every LatexEnvironmentNode has a nodelist            
            verbatim_contents =  joinNodesVerbatim(node.nodelist) 
            joined_whole = (
                rf'\begin{{{node.envname}}}'
                rf'{verbatim_args}'
                rf'{verbatim_contents}'
                rf'\end{{{node.envname}}}'
            )
            joined_regex = (
                rf'^\\begin(\s*){{{re.escape(node.envname)}}}(\s*)'
                rf'{re.escape(verbatim_args)}'
                rf'{re.escape(verbatim_contents)}'
                rf'\\end(\s*){{{re.escape(node.envname)}}}$'
            )
            re_res = re.search(joined_regex, node_verbatim)
            if re_res is None:
                err_message = "Environment node was malformed or parsed incorrectly"
                logger.critical(err_message)
                partial_regex = joined_regex
                span = None
                while span is None:
                    try:
                        m = re.search(partial_regex, node_verbatim)
                        if m is not None:
                            span = m.span()
                    finally:
                        partial_regex = partial_regex[:-1]
                with open("node_verbatim.txt", "w") as f:
                    f.write(node_verbatim)
                with open("joined_regex.txt", "w") as f:
                    f.write(joined_regex)
                with open("matches_until.txt", "w") as f:
                    f.write(partial_regex)
                with open("span.txt", "w") as f:
                    f.write(str(span))
                raise RuntimeError(err_message)
            
            aft_begin_sp, bef_args_sp, aft_end_sp = re_res.groups()
                
            if node.envname in allowed_environments or node.envname in ONLY_MARK_CAPTION_ENVS:
                marked_contents = []
                is_distinct_mark_env = node.envname in DISTINCTLY_MARKED_ENVIRONMENTS
                
                if is_distinct_mark_env:
                    parent_counter_keys.append(node.envname)

                this_prev_node = None
                for nested_node in node.nodelist:
                    marked_contents.append(
                        recMark(
                            nested_node,
                            node,
                            this_prev_node,
                            parent_counter_keys
                        )
                    )
                    this_prev_node = nested_node
                    
                if is_distinct_mark_env:
                    parent_counter_keys.pop()
                    
                return (
                    rf"\begin{aft_begin_sp}{{{node.envname}}}{bef_args_sp}"
                    rf"{verbatim_args}"
                    rf"{''.join(marked_contents)}"
                    rf"\end{aft_end_sp}{{{node.envname}}}"
                )
            else:
                return node_verbatim
            
        elif isinstance(node, LatexMacroNode):
            if (node.macroname in MARKED_ENTIRE_CSNAMES
                and (prev_node is not None)
                and (not is_in_only_mark_caption_env)
                and not in_preamble):
                prev_ends_italcorr = re.search(
                    r'[(\[]\s*$',
                    prev_node.latex_verbatim()
                )
                if prev_ends_italcorr is None:
                    return markStr(node_verbatim, parent_counter_keys)
                else:
                    return node_verbatim
            elif node.macroname in DISTINCTLY_MARKED_MACROS:
                if is_in_only_mark_caption_env and node.macroname != 'caption':
                    return node_verbatim
                # process args vvv
                len_arg_spec = len(CSNAMES_ARGSPEC[node.macroname])
                argnlist = node.nodeargd.argnlist
                
                joined_verbatim_macro = rf"\{node.macroname}"
                joined_verbatim_macro += ''.join([
                    n.latex_verbatim() if n is not None else ''
                    for n in argnlist
                ])
                
                if node_verbatim != joined_verbatim_macro:
                    logger.warning(
                        f"{node.macroname} verbatim, "
                        f"\n```latex\n{node_verbatim}\n```\ndid not "
                        f"match joined verbatim\n"
                        f"```latex\n{joined_verbatim_macro}\n```\nignoring..."
                    )
                    return node_verbatim
                elif len(argnlist) != len_arg_spec:
                    logger.warning(
                        f"{node.macroname}, {node_verbatim}, "
                        f"did not match argspec len: "
                        f"{len(argnlist)} != {len_arg_spec}"
                    )
                    return node_verbatim
                
                parent_counter_keys.append(node.macroname)                
                marked_macro = [rf"\{node.macroname}"]
                this_prev_node = None
                for idx, arg_node in enumerate(argnlist):
                    if arg_node is None:
                        this_prev_node = arg_node
                        continue
                    if idx == DISTINCTLY_MARKED_MACROS[node.macroname]:
                        marked_macro.append(
                            recMark(arg_node, node, this_prev_node, parent_counter_keys)
                        )
                    else:
                        marked_macro.append(arg_node.latex_verbatim())
                    this_prev_node = arg_node
                    
                parent_counter_keys.pop()
                
                return ''.join(marked_macro)
                # ^^^
            else:
                return node_verbatim

        elif in_preamble and not parent_counter_keys:
            return node_verbatim
        
        elif isinstance(node, LatexMathNode):
            if node.displaytype == 'inline' and not is_in_only_mark_caption_env:
                return markStr(node_verbatim, parent_counter_keys)
            else:
                return node_verbatim
            
        elif isinstance(node, LatexCharsNode):
            if is_in_only_mark_caption_env:
                return node_verbatim

            # don't mark key=chars in bib third argument contents
            if in_bib and '=' in node_verbatim:
                return node_verbatim
            
            # mark every span in safe envs
            pattern = chars_node_match_regex
            maybe_restrict_pattern_or_node_verbatim = (
                prev_node is not None and
                (prev_node.isNodeType(LatexMacroNode)
                 or prev_node.isNodeType(LatexGroupNode)
                 or prev_node.isNodeType(LatexCommentNode))
            )
            untouched_start = ''
            if maybe_restrict_pattern_or_node_verbatim:
                # restrict node verbatim
                prev_is_accent_macro = (
                    prev_node.isNodeType(LatexMacroNode)
                    and prev_node.macroname in ACCENT_MACROS
                )
                if prev_is_accent_macro:
                    m = re.search(r'^\s*\S+(?=\s)', node_verbatim)
                    if m is None:
                        return node_verbatim
                    m_start, m_end = m.span()
                    untouched_start = node_verbatim[:m_end]
                    node_verbatim = node_verbatim[m_end:]
                # restrict pattern
                allow_left_match_begin = (
                    parent_is_distinctly_marked_macro or
                    (prev_node.isNodeType(LatexMacroNode) and
                     prev_node.macroname in OTHER_PREV_MACRO_NODE_EXCEPTIONS)
                )
                if not allow_left_match_begin:
                    left = r"[\n\t $(~]"
                    inside = r"[a-zA-Z0-9!?.,'`/;:\-()@]+"
                    right = r"[\n\t $)~]"
                    # otherwise, pattern is rf"(?:(?<={left})|^){inside}(?:(?={right})|$)"
                    pattern = rf"(?<={left}){inside}(?:(?={right})|$)"
                    
            marked_str, num_subs = re.subn(
                pattern,
                lambda m: markStr(m.group(0), parent_counter_keys),
                node_verbatim
            )                
            return untouched_start + marked_str
        
        elif isinstance(node, LatexGroupNode):
            if is_in_only_mark_caption_env:
                return node_verbatim
            
            if prev_node is not None:
                prev_ends_square = re.search(
                    r'[\]\}]\s*$',
                    prev_node.latex_verbatim()
                )
                prev_includes_forbidden_mark_bib_keys = re.search(
                    '|'.join(FORBIDDEN_BIB_KEYS),
                    prev_node.latex_verbatim(),
                    flags=re.IGNORECASE
                )
                
                if (in_bib
                    and prev_includes_forbidden_mark_bib_keys is not None):
                    return node_verbatim
            
            if ((prev_node is not None
                and prev_node.isNodeType(LatexCharsNode)
                and prev_ends_square is None)
                or parent_is_distinctly_marked_macro):
                # every LatexGroupNode has a nodelist                
                verbatim_contents = joinNodesVerbatim(node.nodelist) 
                
                joined_whole = rf'{{{verbatim_contents}}}'
                if node_verbatim != joined_whole:
                    err_message = (
                        f"Group node '{node_verbatim}' in markNode "
                        "was malformed or parsed incorrectly"
                    )
                    logger.debug(f"{verbatim_contents} != {joined_whole}")
                    logger.critical(err_message)
                    raise RuntimeError(err_message)

                marked_contents = []
                this_prev_node = None
                for nested_node in node.nodelist:
                    marked_contents.append(recMark(
                        nested_node,
                        node,
                        this_prev_node,
                        parent_counter_keys
                    ))
                    this_prev_node = nested_node
                return "{" + ''.join(marked_contents) + "}"
            else:
                return node_verbatim
            
        elif isinstance(node, LatexCommentNode):
            return node_verbatim
        
        else:
            logger.warning(
                f"Unrecognized latex node '{node.nodeType()}'"
                f"during markNode(); writing "
                f"node.latex_verbatim(): '{node_verbatim}'"
            )
            return node_verbatim

    # markNodes body
    manuscript_marked_contents = []
    outer_prev_node = None
    for start_node in to_mark_nodelist:
        manuscript_marked_contents.append(
            recMark(
                start_node,
                None,
                outer_prev_node,
                []
            )
        )
        outer_prev_node = start_node
    return ''.join(manuscript_marked_contents)

def getPreambleAndDocument(nodelist):
    num_document_envs = sum(
        1 for n in nodelist
        if n.isNodeType(LatexEnvironmentNode)
        and n.envname == 'document'
    )
    if num_document_envs != 1:
        err_message = (
            fr"\begin{{document}} appeared {num_document_envs} times"
        )
        logger.critical(err_message)
        raise RuntimeError(err_message)

    i = 0
    # so I've been assuming that the first environment of the
    # file is the document environment. That's alright actually
    while nodelist[i].nodeType() != LatexEnvironmentNode:
        i += 1
        
    preamble_nodes = nodelist[:i]
    document_node = nodelist[i]
    post_document_nodes = nodelist[i+1:]

    return preamble_nodes, document_node, post_document_nodes

def splitPreambleNodes(preamble_nodes: list[LatexNode]):
    for i, node in enumerate(preamble_nodes):
        if (isinstance(node, LatexMacroNode)
            and node.macroname == 'documentclass'):
            return preamble_nodes[:i+1], preamble_nodes[i+1:]
    err_message = ("\\documentclass command not found")
    logger.critical(err_message)
    raise RuntimeError(err_message)

def texPointsToPDFpoints(tex_pts: float) -> float:
    return tex_pts * TEX_POINTS_TO_PDF_POINTS_CONVERSION_RATIO

def scaledPointsToPDFpoints(sp: int) -> float:
    tex_pts = sp / SCALED_POINTS_PER_TEX_POINT
    return texPointsToPDFpoints(tex_pts)

# these two unzips could be made more readable
def unzipHbox(hbox):
    return (
        hbox[0],
        tuple(map(lambda pts: texPointsToPDFpoints(float(pts)), hbox[1:]))
    )

def unzipPos(stend_xy):
    return (
        stend_xy[0],
        tuple(map(lambda spts: scaledPointsToPDFpoints(int(spts)), stend_xy[1:-1]))
    )

def boxinfoToPDFRectangle(hbox, start_xy):
    pgA, (width, height, depth) = unzipHbox(hbox)
    pgB, (x0, sy) = unzipPos(start_xy)
    
    return (
        pgB,
        pymupdf.Rect(x0, sy - height, x0 + width, sy + depth)
    )
        
def getWordBoxes(boxpositions_filename: Path):
    word_boxes = dict()
    not_colon = r'([^:]*)'

    with open(boxpositions_filename, 'r') as f:
        line = f.readline().strip()
        line_no = 1
        while line:
            box_info = re.match(
                fr"^{not_colon}:(pwhd|spxy):{not_colon}:{not_colon}:{not_colon}:{not_colon}$",
                line,
                flags=re.IGNORECASE
            )
            if box_info is None:
                err_message = (
                    f"Line {line_no} of {boxpositions_filename} "
                    f"'{repr(line)}' did not match the info spec"
                )
                logger.critical(err_message)
                raise RuntimeError(err_message)
            matches = box_info.groups()
            (key, label, values) = (
                matches[0],
                matches[1],
                tuple(map(lambda m: m.strip('pt'), matches[2:]))
            )
            
            if key in word_boxes:
                if label in word_boxes[key] and word_boxes[key][label] != values:
                    # if the title hbox or position appears more
                    # than once with new values from being rewritten
                    # in the running head, we just ignore said
                    # the new values                    
                    if 'TITLE' in key:
                        line = f.readline().strip()
                        line_no += 1
                        continue
                    # for some reason, when using the prdlatex format
                    # (even with texlive2024), caption boxes will appear
                    # twice (or perhaps more times)
                    # with different box information, but it looks like the
                    # second (or last) appearance is correct.
                    # This is probably fragile.
                    if 'CAPTION' in key:
                        word_boxes[key][label] = values
                        line = f.readline().strip()
                        line_no += 1
                        continue                    
                    # There should be only two labels (and they should
                    # each appear at most once):
                    # Captions are read in twice, so it's okay if a label
                    # for a key appears more than once if the values
                    # are the same
                    # 'pwhd' for page, width, height, depth;
                    # and 'spxy' for start, page, x pos, y pos
                    # see above comment for when they aren't the same
                    err_message = (
                        f"Key '{key}' with label '{label}' was already "
                        f"in '{word_boxes[key]}' and values differed\n"
                        f"current value:\n```python\n"
                        f"{word_boxes[key][label]}\n```\n"
                        f"new value:\n```python\n{values}\n```"
                    )
                    logger.critical(err_message)
                    raise RuntimeError(err_message)
                word_boxes[key][label] = values
            else:
                word_boxes[key] = {label:values}
                
            line = f.readline().strip()
            line_no += 1

    markids_to_delete = set()
    
    for key, info in word_boxes.items():
        if 'spxy' not in info:
            logger.warning(
                f"Could not extract individual box information: "
                f"box with mark id '{key}' is not written to the PDF"
            )
            markids_to_delete.add(key)
            continue            
        if len(info) != 2:
            # all marks should have exactly two fields (except for
            # the ones which don't make it to the page)
            # the hbox dimensions and the start page and xy positions
            err_message = (
                f"mark id '{key}' in '{boxpositions_filename}' "
                "differed from specification"
            )
            logger.critical(err_message)
            raise RuntimeError(err_message)

    for mark_id in markids_to_delete:
        del word_boxes[mark_id]

    tex_word_boxes = dict()
    for key, info in word_boxes.items():
        (page_label, rectangle) = boxinfoToPDFRectangle(
            info['pwhd'],
            info['spxy']
        )
        
        if page_label in tex_word_boxes:
            tex_word_boxes[page_label][key] = rectangle
        else:
            tex_word_boxes[page_label] = {key:rectangle}

    num_boxes = sum(
        len(markids_to_rectangles)
        for markids_to_rectangles in tex_word_boxes.values()
    )
    logger.info(f"Saved {num_boxes} tex word boxes")
    
    return tex_word_boxes, markids_to_delete

def readBalancedBraces(idx: int, s: str) -> tuple[str, int]:
    """Read unescaped balanced braces, return (contents without outer braces, chars_read)."""
    depth = 1  # Already past opening brace
    start_idx = idx
        
    while idx < len(s) and depth > 0:
        if s[idx] == '\\' and idx + 1 < len(s):
            idx += 2
        else:
            if s[idx] == '}':
                depth -= 1
            elif s[idx] == '{':
                depth += 1
            idx += 1
            
    if depth != 0:
        err_message = ('Unbalanced curly braces in marked string')
        logger.critical(err_message)
        raise RuntimeError(err_message)
    
    contents = s[start_idx:idx-1]
    return contents, idx - start_idx

def unMarkWithPositions(
        marked_string: str,
        deleted_mark_IDs: set[str]
) -> tuple[str, dict[str, tuple[int, int]]]:
    r"""Remove \markbox commands and track positions
    of marked content.
    
    Returns:
        (unmarked_string, mark_positions) where
    mark_positions maps mark_id -> (start, end)
        Positions are in the unmarked string,
    with end being exclusive.
    """
    
    mark_positions = {}
    current_pos = 0  # Position in unmarked string
    idx = 0
    MARKBOX = MARK_CSNAME + '{'
    MARKBOX_LEN = len(MARKBOX)

    unmarked_strs = []
    
    while idx < len(marked_string):
        if marked_string[idx:idx+MARKBOX_LEN] == MARKBOX:
            idx += len(MARKBOX)
          
            (mark_id, chars_read) = readBalancedBraces(idx, marked_string)
            idx += chars_read + 1  # +1 for opening brace of second arg
          
            (content, chars_read) = readBalancedBraces(idx, marked_string)
            
            # Track position in unmarked string
            start_pos = current_pos
            end_pos = current_pos + len(content)

            # don't bother tracking deleted mark IDs
            if mark_id not in deleted_mark_IDs:
                mark_positions[mark_id] = (start_pos, end_pos)
            
            unmarked_strs.append(content)
            current_pos += len(content)
            idx += chars_read
        else:
            unmarked_strs.append(marked_string[idx])
            current_pos += 1
            idx += 1
    
    return ''.join(unmarked_strs), mark_positions

def numericComponent(s: str) -> int:
    return int(''.join(filter(str.isnumeric, s)))

def alphaComponent(s: str) -> str:
    return ''.join(filter(str.isalpha, s))

def markIdToCountInfo(mark_id: str) -> list[dict[str, int]]:
    counts = mark_id.split(',')
    count_info = []
    for count in counts:
        head_and_stem = count.split(';')
        if len(head_and_stem) != 2:
            raise ValueError(
                f"A segment of a nested mark '{head_and_stem}' "
                "was not delimited into two by a semicolon"
            )
        count_name = alphaComponent(head_and_stem[0])
        head_count = numericComponent(head_and_stem[0])
        stem_count = int(head_and_stem[1])
        count_info.append(
            {'name': count_name,
             'head': head_count,
             'stem': stem_count}
        )
    return count_info

def validateMarkPositions(
        mark_positions: dict[str, tuple[int, int]],
        tex_word_boxes: dict[int, dict[str, pymupdf.Rect]]
) -> None:
    """Verify that no mark positions overlap and that
    the mark position keys are a superset of the
    document word box keys. Raises ValueError if they do.
    
    Also verify that tex_word_boxes.keys() is
    a subset of in mark_positions.keys()
    
    Args:
        mark_positions:
    dict mapping mark_id -> (start, end) positions
    Raises:
        ValueError: if any two marks overlap
    """
    # Sort by start position
    sorted_marks = sorted(
        mark_positions.items(),
        key=lambda x: x[1][0]
    )

    for mark_id in mark_positions:
        count_info = markIdToCountInfo(mark_id)
        if (count_info[0]['name'] == 'document'
            and count_info[0]['head'] != 0):
            raise ValueError(
                f"Unexpected mark ID '{mark_id}': "
                f"document head is greater than 0"
            )
    
    # Check each adjacent pair
    for i in range(len(sorted_marks) - 1):
        id_i, (start_i, end_i) = sorted_marks[i]
        id_j, (start_j, end_j) = sorted_marks[i + 1]
        
        if start_i >= end_i:
            raise ValueError(
                f"Invalid mark position for '{id_i}': "
                f"start ({start_i}) >= end ({end_i})"
            )
        
        # Since sorted by start, only need to
        # check if previous end > next start
        if start_j < end_i:
            raise ValueError(
                f"Mark positions overlap: "
                f"mark '{id_i}' at [{start_i}, {end_i}) overlaps with "
                f"mark '{id_j}' at [{start_j}, {end_j})"
            )
    
    if sorted_marks:
        id_last, (start_last, end_last) = sorted_marks[-1]
        if start_last >= end_last:
            raise ValueError(
                f"Invalid mark position for '{id_last}': "
                f"start ({start_last}) >= end ({end_last})"
            )

    # check that mark_positions.keys() is a superset of all tex_word_boxes mark_ids
    for page_boxes in tex_word_boxes.values():
        for mark_id in page_boxes.keys():
            if mark_id not in mark_positions:
                raise ValueError(
                    f"mark_id '{mark_id}' not in mark_positions. "
                    "mark_positions.keys() is not a superset of "
                    "all mark_ids in tex_word_boxes."
                )

    logger.info("Mark positions are valid")

def parseLatex(
        tex_str: str
) -> tuple[list[LatexNode], list[LatexNode], LatexNode, list[LatexNode]]:
    # Set up parser context with recognized commands and environments
    latex_context = LatexContextDb()
    macro_specs = [
        std_macro(csname, args_format)
        for csname, args_format in CSNAMES_ARGSPEC.items()
    ]
    environment_specs = [
        std_environment(envname, args_format)
        for envname, args_format in ENVNAMES_ARGSPEC.items()
    ]
    
    latex_context.add_context_category(
        'markspec',
        macros=macro_specs,
        environments=environment_specs
    )

    # Add Ignore Parsing
    custom_macros = [
        MacroSpec(
            'startignorepylatexenc',
            args_parser=IgnoreRegionArgsParser(),
        )
    ]
    
    latex_context.add_context_category(
        'ignore-regions',
        macros=custom_macros,
        prepend=True,
    )
    
    # Parse LaTeX --> get LaTeX node list 
    (nodelist, _, _) = LatexWalker(tex_str, latex_context=latex_context).get_latex_nodes(pos=0)

    if joinNodesVerbatim(nodelist) != tex_str:
        err_message = (
            f"TeX source was not preserved after LatexWalker parsing"
        )
        logger.critical(err_message)
        raise RuntimeError(err_message)
           
    (preamble_nodes, document_node, post_document_nodes) = getPreambleAndDocument(nodelist)

    # update preamble_nodes, exlude docclass
    (docclass_nodes, preamble_nodes) = splitPreambleNodes(preamble_nodes)

    return docclass_nodes, preamble_nodes, document_node, post_document_nodes

def getInsertedCodeForMarking(boxpositions_filename: str) -> tuple[str, str]:
    r"""
    for some reason \leavevmode's expansion of \unhbox\voidb@x
    is sometimes not read with @ as a letter, resulting in
    the error `undefined control sequence \voidb<linebreak>@x`.
    Should probably be looked into.

    To get the correct pdfposition, I must leave vmode when marking.
    Otherwise the position recorded is before the new paragraph
    """
    tex_write_commands = textwrap.dedent(
        rf"""
        \makeatletter       
        \let\voidb\voidb@x
        \makeatother

        \newwrite\markfile
        \immediate\openout\markfile={boxpositions_filename}
        """
    )

    markcs_def = textwrap.dedent(
        rf"""
        \newcommand{{{MARK_CSNAME}}}[2]{{%
        \ifvmode\leavevmode\fi%
        \setbox0=\hbox{{#2}}%
        \immediate\write\markfile{{#1:pwhd:\thepage:\the\wd0:\the\ht0:\the\dp0}}%
        \pdfsavepos%
        \write\markfile{{#1:spxy:\thepage:\the\pdflastxpos:\number\dimexpr\pdfpageheight-\pdflastypos sp\relax:}}%
        #2%
        }}
        """
    )
    return tex_write_commands, markcs_def

def markLatex(latex_file: Path, *parse_out, **opt):
    extra_mark_envs = opt['extra_mark_envs']
    extra_mark_envs = set(extra_mark_envs.split(','))
    
    (docclass_nodes, preamble_nodes, document_node, post_document_nodes) = parse_out

    # Track \newtheorems
    enunciations = getEnunciations(preamble_nodes)
    enunciation_names = set(enun['name'] for enun in enunciations)
    all_allowed_mark_environments = ALLOWED_MARK_ENVIRONMENTS.union(enunciation_names).union(extra_mark_envs)    

    # build LatexCharsNode node marking regexes
    left = r"[\n\t $(~]"
    inside = r"[a-zA-Z0-9!?.,'`/;:\-()@]+"
    right = r"[\n\t $)~]"
    chars_node_match_regex = rf"(?:(?<={left})|^){inside}(?:(?={right})|$)"

    marked_preamble = markNodes(
        preamble_nodes,
        all_allowed_mark_environments,
        chars_node_match_regex
    )
        
    marked_document = markNodes(
        [document_node],
        all_allowed_mark_environments,
        chars_node_match_regex
    )

    boxpositions_filename = f'boxpositions_{latex_file.stem}.txt'
    (tex_write_commands, markcs_def) = getInsertedCodeForMarking(boxpositions_filename)

    # build marked file out of components    
    docclass_str = joinNodesVerbatim(docclass_nodes)
    post_document_str = joinNodesVerbatim(post_document_nodes)
    marked_tex = docclass_str + tex_write_commands + markcs_def + marked_preamble + marked_document + post_document_str

    # Save the marked file to the same directory as the input latex_file, compile it, then move it
    marked_filename = latex_file.parent / f"{latex_file.stem}_marked{latex_file.suffix}" # automatically Path object from / 
    utils.writeStringToFile(marked_tex, marked_filename)

    return marked_preamble, marked_document, marked_filename, boxpositions_filename
    
    
def getSyncInfo(latex_file: Path, **opt):
    tex_str = utils.sourceAsString(latex_file)        

    # Parse
    logger.info(f"Parsing {latex_file}...")    
    parse_out = parseLatex(tex_str)
    logger.info("Done")    

    _, _, document_node, _  = parse_out

    # Mark
    logger.info("Inserting marks...")
    marked_preamble, marked_document, marked_filename, boxpositions_filename = markLatex(
        latex_file,
        *parse_out,
        **opt
    )
    logger.info("Done")

    # Compile and diff-pdf
    if opt['validate']:
        process1 = utils.compile_tex(latex_file, opt['compiler'])
        
    process2 = utils.compile_tex(marked_filename, opt['compiler'])

    tmp_dir = Path('tmp_marktex')
    Path.mkdir(tmp_dir, exist_ok = True)
    utils.transfer_tex_files(latex_file, tmp_dir, 'cp')
    utils.transfer_tex_files(marked_filename, tmp_dir, 'mv')    
    moved_boxpositions_filename = (latex_file.parent/boxpositions_filename).move_into(tmp_dir)

    orig_pdf = utils.pdf_name(latex_file)
    marked_pdf = utils.pdf_name(marked_filename)

    if opt['validate']:
        process3, _ = utils.run_diff_pdf(
            orig_pdf,
            marked_pdf,
            tmp_dir
        )
        logger.info(f"{orig_pdf} and {marked_pdf} are within tolerance")
    else:
        logger.info(f"Did not compare {orig_pdf} and {marked_pdf} (--no-validate)")

    # Retrieve box info
    logger.info("Getting word boxes...")
    tex_word_boxes, deleted_mark_IDs = getWordBoxes(tmp_dir / boxpositions_filename)
    logger.info("Done")    

    logger.info("Unmarking LaTeX...")
    # Do not concatenate the inserted code for marking
    docclass_nodes, post_document_nodes = parse_out[0], parse_out[-1]
    docclass_str = joinNodesVerbatim(docclass_nodes)
    post_document_str = joinNodesVerbatim(post_document_nodes)    
    
    unmarked_str, mark_positions = unMarkWithPositions(
        docclass_str + marked_preamble + marked_document + post_document_str, deleted_mark_IDs
    )
    logger.info("Done")

    if unmarked_str != tex_str:
        err_message = ("Unmarked LaTeX does not match input LaTeX")
        logger.critical(err_message)
        raise RuntimeError(err_message)

    validateMarkPositions(mark_positions, tex_word_boxes)

    if opt['clean']:
        utils.removeDir(tmp_dir)

    return mark_positions, tex_word_boxes, document_node.pos
