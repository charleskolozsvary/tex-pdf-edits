import logging
logger = logging.getLogger(__name__)

from texpdfedits.corr import Correction
from texpdfedits.extractanns import XrefObj
import texpdfedits.utils as utils

import re

DOWN_SYMBOL = "⭣"
UP_SYMBOL = "⭡"
JOIN_SYMBOL = " "

SYMBOL_NUM = 3

START_SNIPPET = JOIN_SYMBOL.join(DOWN_SYMBOL for _ in range(SYMBOL_NUM))
END_SNIPPET = JOIN_SYMBOL.join(UP_SYMBOL for _ in range(SYMBOL_NUM))

REMOVE_REGEX = re.compile(
    rf"""
    %%                                             \n
    ^%%\ Annotation\ [0-9]+, [^\n]*+ \n
    (?:^% [^\n]*+ \n)+?
    ^%{re.escape(START_SNIPPET)}               [^\n]*+ \n
    (?P<empty_line_start>[ \t\r]*\n)?+               
    (?P<latex>.*?)                                   
    %%                                             \n
    ^%{re.escape(END_SNIPPET)}               [^\n]*+ \n
    (?P<empty_line_end>[ \t\r]*\n)?+
    """,
    flags=re.VERBOSE | re.DOTALL | re.MULTILINE
)

USE_UNICODE_STATUS = True

DELETE_TAG = 'nocomments'

def status_to_unicode(status: str | None):
    match status:
        case XrefObj.STATUS_NONE:
            return status
            # return b'\xe2\x88\x85'.decode('utf-8')
            # return b'\xf0\x9f\xaa\xb9'.decode('utf-8')
        case XrefObj.STATUS_ACCEPTED:
            return b'\xf0\x9f\x96\x92'.decode('utf-8')
            # return b'\xf0\x9f\x91\x8d'.decode('utf-8')
        case XrefObj.STATUS_REJECTED:
            # return status
            return b'\xf0\x9f\x91\x8e'.decode('utf-8')
            # return b'\xe2\x9c\x8b'.decode('utf-8')
        case XrefObj.STATUS_CANCELLED:
            return b'\xf0\x9f\x9a\xab'.decode('utf-8')
        case XrefObj.STATUS_COMPLETED:
            return b'\xe2\x9c\x94'.decode('utf-8')
            # return b'\xe2\x9c\x85'.decode('utf-8')
            # return b'\xe2\x9c\x8c'.decode('utf-8')
        case XrefObj.STATUS_DEFERRED:
            return b'\xe2\x8f\xb3'.decode('utf-8')
        case XrefObj.STATUS_FUTURE:
            return b'\xf0\x9f\x95\x90'.decode('utf-8')
        case _:
            return '???'

def get_replies_and_status(corr: Correction, replies: str) -> str:
    if replies:
        replies = f'\n%% Replies: "{replies}"'

    status_message = '(AUTOCORRECTED) [ ]' if corr.is_autocorrected else '[ ]'

    if corr.checkmark is None:
        checkmark = ''
    else:
        checkmark = corr.checkmark.state
    if corr.status is None:
        status = ''
    else:
        status = corr.status.state
    if checkmark:
        status_message += ' (✔)' if checkmark == XrefObj.CHECKED else ' ( )'
    if status:
        status_message += f' ({status_to_unicode(status)})' if USE_UNICODE_STATUS else f' ({status})'

    return replies, status_message

def startComment(corr: Correction, replies: str) -> str:
    corr_tid, corr_type = corr.type    
    replies, status_message = get_replies_and_status(corr, replies)
    within_page = f"{corr.nested_count.value}/{corr.nested_count.total} on"

    return (
        f"%% Annotation {corr.index}, {within_page} page {corr.pageno+1} {status_message}\n"
        f"%% {corr_type}: \"{utils.sanitize_pdf_text(corr.pdf_selected_text)}\"\n"
        f"%% Comment: \"{utils.sanitize_pdf_text(corr.messages['comment'])}\"{replies}\n"
        f"%%\n"
    )

def endComment(corr: Correction, replies: str) -> str:
    return ''

def writeCallout(corr_idxs: list[int], start_or_end: str) -> str:
    sing_plural = 'annotation' if len(corr_idxs) == 1 else 'annotations'
    
    if start_or_end == "start":
        return f'%{START_SNIPPET}\n'
    else:
        return (
            f'%{END_SNIPPET} {start_or_end.upper()} of {sing_plural} '
            + ', '.join(str(idx) for idx in corr_idxs)
            + '\n'
        )

def deleteComments(tex_file: Path) -> tuple[str, Path]:
    tex_str = utils.sourceAsString(tex_file)
    n_newnew_start = 0
    n_newnew_end = 0

    def doReplace(match):
        nonlocal n_newnew_start
        nonlocal n_newnew_end
        replacement = []
        if match.group("empty_line_start") is not None:
            replacement.append("\n\n")
            n_newnew_start += 1
        replacement.append(match.group("latex"))
        if match.group("empty_line_end") is not None:
            replacement.append("\n\n")
            n_newnew_end += 1
        return ''.join(replacement)

    nocomments_tex_str, n_subs1 = REMOVE_REGEX.subn(doReplace, tex_str)
    nocomments_tex_str, n_subs2 = REMOVE_REGEX.subn(doReplace, nocomments_tex_str)
    
    logger.info(f"Deleted {n_subs1 + n_subs2} comments")
    logger.debug(f"{n_newnew_start} double newlines after start")
    logger.debug(f"{n_newnew_end} double newlines after end")
    
    nocomments_file = utils.tagFileStem(tex_file, DELETE_TAG)
    utils.writeStringToFile(nocomments_tex_str, nocomments_file)
    return nocomments_tex_str, nocomments_file
