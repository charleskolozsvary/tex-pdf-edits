import logging
logger = logging.getLogger(__name__)
import argparse
import re
import sys
import subprocess
from pathlib import Path

import texpdfedits.extractanns as extractanns
import texpdfedits.modifytex as modifytex
import texpdfedits.corr as corr
import texpdfedits.utils as utils
import texpdfedits.formatcomm as formatcomm
import texpdfedits.svn as svn

from importlib.metadata import version
__version__ = version('texpdfedits')

def process_files(pdf_file: Path, latex_file: Path, **opt):
    tex_str = utils.sourceAsString(latex_file)    
    
    if opt['svn']:
        svn.verify_status_clean(latex_file)

    if opt['delete_comments']:
        logger.info(f"Deleting comments from {latex_file}...")
        _, nocomments_file = formatcomm.deleteComments(latex_file)
        logger.info(f"Done. Written to {nocomments_file}")

        utils.compile_validate_clean_replace(
            latex_file,
            nocomments_file,
            Path('./'),
            **opt
        )
        if opt['svn']:
            svn.commit(latex_file, f'removed annotation comments from {latex_file} [annin -dc]')
        return
        
    corrections, overlapping_keys, n_annots, n_edits, n_no_locate = corr.getCorrections(
        pdf_file,
        latex_file,
        **opt,
    )

    n_corrs = len(corrections)
    
    char_positions, charpos_to_kinds_and_corrections = modifytex.getSourcePosToCorrections(corrections)
    
    commented_latex_file = Path(f"{latex_file.parent / latex_file.stem}_{utils.INLINED_TAG}.tex")
    commented_tex_str = modifytex.commentSource(
        tex_str,
        char_positions,
        charpos_to_kinds_and_corrections,
        **opt
    )
    utils.writeStringToFile(commented_tex_str, commented_latex_file)

    cwd = Path('./')

    utils.compile_validate_clean_replace(
        latex_file,
        commented_latex_file,
        cwd,
        compile_file1 = False,
        **opt
    )

    # subtract corrections that could not be located for log summary
    summary_log = f"n_annots: {n_annots}, n_edits: {n_edits}, n_corrs: {n_corrs - n_no_locate}"

    if not opt['auto']:
        logger.info(summary_log)
        if opt['svn']:
            svn.commit(latex_file, f'wrote annotations from {pdf_file} in {latex_file} [annin]')
        return 

    logger.info("Doing autocorrections...")
    corrected_snippets = modifytex.getCorrectedSnippets(corrections, overlapping_keys)
    logger.info("Done")
    
    autocorrected_tex_str = modifytex.commentSource(
        tex_str,
        char_positions,
        charpos_to_kinds_and_corrections,
        corrected_snippets = corrected_snippets,
        **opt
    )
    n_autos = sum(1 for corr in corrections if corr.is_autocorrected)
    
    autocorrected_latex_file = Path(f"{latex_file.stem}_{utils.AUTO_TAG}.tex")
    utils.writeStringToFile(autocorrected_tex_str, autocorrected_latex_file)

    logger.info(f"Autocorrected {n_autos:3d}/{len(corrections):3d} corrections")
    logger.info(f"{summary_log}, n_autos: {n_autos}")    

    if not opt['replace']:
        return
    
    autocorrected_compiles = True
    try:
        utils.compile_validate_clean_replace(
            latex_file,
            autocorrected_latex_file,
            cwd,
            compile_file1 = False,
            **opt
        )
    except subprocess.CalledProcessError as e:
        logger.warning(f"{e}\n{autocorrected_latex_file} failed to compile")
        autocorrected_compiles = False
    except FileNotFoundError as e:
        logger.error(f"{e}\n{autocorrected_latex_file} did not generate expected output")
        autocorrected_compiles = False

    if opt['svn'] and autocorrected_compiles:
        auto_message_flag = ' --auto' if n_autos > 0 else ''
        commit_message = (
            f'wrote annotations from {pdf_file} in '
            f'{latex_file} [annin{auto_message_flag}]'
        )
        svn.commit(latex_file, commit_message)
    
    return

def ProgramBanner():
    script_name = Path(sys.argv[0]).name
    return f"This is {script_name} version {__version__}"

def main():
    parser = argparse.ArgumentParser(
        description = f'writes PDF annotations in LaTeX source as comments'
    )

    parser.add_argument('pdf_file')
    parser.add_argument('latex_file', nargs='?', default=None)

    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}"
    )

    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help='debugging output'
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help='set logging level only warnings or greater'
    )
    
    parser.add_argument(
        "--svn",
        action=argparse.BooleanOptionalAction,
        help='perform svn operations; default=True',
        default=True,
    )
    parser.add_argument(
        "--validate",
        action=argparse.BooleanOptionalAction,
        help='validate results (run diff-pdf); default=True',
        default=True,
    )    
    parser.add_argument(
        "--merge-overlapping",
        action=argparse.BooleanOptionalAction,
        help='merge overlapping corrections; default=True',
        default=True
    )
    parser.add_argument(
        "--clean",
        action=argparse.BooleanOptionalAction,
        help='delete intermediate files and tmp dirs; default=True',
        default=True
    )
    parser.add_argument(
        "--replace",
        action=argparse.BooleanOptionalAction,
        help='overwrite latex file; default=True',
        default=True
    )
    parser.add_argument(
        "-a",
        "--auto",
        action="store_true",
        help='perform simple annotations automatically; default=False'
    )
    parser.add_argument(
        "--adjust-annots",
        action="store_true",
        help=('adjust annotation rectangles; default=False'),
    )
    parser.add_argument(
        "-dc",
        "--delete-comments",
        action="store_true",
        help=('remove annin comments; default=False'),
    )

    parser.add_argument(
        "--compiler",
        type=str,
        help='latex_file compiler; default=pdflatex',
        default=utils.DEFAULT_LATEX_COMPILER
    )    
    parser.add_argument(
        "-eme",
        "--extra-mark-envs",
        type=str,
        help=('comma-separated additional environments to mark; default=\'\''),
        default=''
    )
    parser.add_argument(
        "-s",
        "--tex-start",
        type=str,
        help=(
            'first page of PDF generated by latex_file that '
            'corresponds to first page of pdf_file '
            '(not absolute, use rendered page label); '
            'default=\'\''
        ),
        default=''
    )
    
    args = parser.parse_args()

    file_log_based_on = args.pdf_file

    script_name = Path(sys.argv[0]).name    
    log_file = utils.newTaggedFname(
        Path(file_log_based_on),
        script_name,
        new_suffix='.log',
        put_front=True,
    )
            
    logger_level = logging.DEBUG if args.debug else logging.INFO
    logger_level = logging.WARN  if args.quiet else logger_level
    
    logging.basicConfig(
        encoding='utf-8',
        level=logger_level,
        format='%(levelname)-8s | %(module)-11s | %(message)s',
        handlers = [
            logging.StreamHandler(),
            logging.FileHandler(log_file, encoding='utf-8', mode='w'),
        ],
    )

    logger.info(ProgramBanner())

    if args.latex_file is None and not args.delete_comments:
        raise RuntimeError("Missing latex_file")

    if args.delete_comments and args.latex_file is None:
        logger.info(f"Treating {args.pdf_file} as latex_file")
        latex_file = args.pdf_file
    else:
        latex_file = args.latex_file    

    if not args.replace and args.svn:
        logger.info("--no-replace disables --svn; not performing svn operations")
        args.svn = False

    if not args.merge_overlapping and args.autocorrect:
        raise RuntimeError("--auto requires --merge-overlapping; enable it or drop --auto")

    if not args.validate and args.replace:
        logger.info(f"--no-validate disables --replace; not overwriting {latex_file}")
        args.replace = False

    if not args.validate and args.svn:
        logger.info(f"--no-validate disables --svn; not performing svn operations")
        args.svn = False

    pdf_file = Path(args.pdf_file)
    latex_file = Path(latex_file)

    if not pdf_file.exists():
        raise FileNotFoundError(f"{pdf_file} does not exist")
        
    if not latex_file.exists():
        raise FileNotFoundError(f"{latex_file} does not exist")

    process_files(
        pdf_file,
        latex_file,
        merge_overlapping = args.merge_overlapping, # left of = are opt names
        compiler          = args.compiler,        
        clean             = args.clean,
        auto              = args.auto,
        adjust_annots     = args.adjust_annots,
        extra_mark_envs   = args.extra_mark_envs,
        delete_comments   = args.delete_comments,
        replace           = args.replace,
        tex_start         = args.tex_start,
        svn               = args.svn,
        validate          = args.validate,
    )
    return

if __name__ == '__main__':    
    main()
