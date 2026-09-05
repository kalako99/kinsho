"""Archive/PDF -> loose-image extraction for the "Auto-extract" library
setting (main.py's `scan_library`/`run_scan`).

This is a faithful port of C:\\de_comp's own `modules/extractor.py` and
`modules/pdf_converter.py` -- same placement rule, same library choices
(zipfile/rarfile for archives, pypdfium2 for PDF) -- per an explicit request
that this feature behave exactly like extracting the same file directly with
the de-comp app, not just "similarly". The one deliberate difference: no
`.7z` support (de-comp has it via py7zr) -- Kinsho's own scanner doesn't
discover `.7z` files as archive candidates at all today (see main.py's
ARCHIVE_EXTENSIONS), so there would never be a `.7z` file to hand this
module in the first place without a separate detection pass this feature
doesn't add. Revisit only if that's ever actually wanted.

Pure filesystem logic, no Kinsho-internal imports (same convention as
comicinfo.py/opds.py/integrity.py) -- main.py decides *which* files to hand
this module and what to do with the result (delete the source, feed the
scanner), this module only knows how to turn one archive/PDF into a folder
of images.
"""
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

try:
    import rarfile
    RAR_SUPPORT = True
except ImportError:
    RAR_SUPPORT = False

try:
    import pypdfium2 as pdfium
    PDF_EXTRACT_SUPPORT = True
except ImportError:
    PDF_EXTRACT_SUPPORT = False

ARCHIVE_EXTENSIONS = {".zip", ".cbz", ".rar", ".cbr"}
PDF_EXTENSIONS = {".pdf"}
PDF_DPI = 150
POINTS_PER_INCH = 72.0


class ProcessingError(Exception):
    """Raised when an archive or PDF can't be inspected or converted."""


class DestinationExistsError(ProcessingError):
    """Raised when the computed output destination already exists."""


def is_extractable(path) -> bool:
    path = Path(path)
    suffix = path.suffix.lower()
    return suffix in ARCHIVE_EXTENSIONS or suffix in PDF_EXTENSIONS


# ------------------------------------------------------------- archives ---
# Ported verbatim from de-comp's extractor.py (plan_extraction's 3-case
# placement rule in particular -- see that module's own docstring for the
# reasoning). Only change: RAR support is checked via RAR_SUPPORT instead of
# an unconditional `import rarfile`, matching main.py's own optional-rarfile
# convention instead of introducing a hard new requirement.

def _top_level_entries(archive_path: Path) -> list[tuple[str, bool]]:
    """Return [(top_level_name, is_dir), ...] for entries at the archive root.

    One entry per distinct top-level name -- a folder containing many nested
    files still yields a single (name, True) pair. A name found nested under
    a path (e.g. "folder/sub/file.txt") counts as a top-level directory even
    if the archive never wrote an explicit directory entry for it.
    """
    suffix = archive_path.suffix.lower()
    entries: dict[str, bool] = {}

    def note(raw_name: str, is_dir_flag: bool) -> None:
        rel = raw_name.replace("\\", "/").strip("/")
        if not rel:
            return
        parts = rel.split("/", 1)
        top = parts[0]
        is_top_level_dir = len(parts) > 1 or is_dir_flag
        entries[top] = entries.get(top, False) or is_top_level_dir

    if suffix in (".zip", ".cbz"):
        with zipfile.ZipFile(archive_path) as zf:
            for info in zf.infolist():
                note(info.filename, info.is_dir())
    elif suffix in (".rar", ".cbr"):
        if not RAR_SUPPORT:
            raise ProcessingError(f"rarfile not installed, cannot inspect {archive_path}")
        with rarfile.RarFile(archive_path) as rf:
            for info in rf.infolist():
                note(info.filename, info.is_dir())
    else:
        raise ProcessingError(f"Unsupported archive type: {archive_path.suffix}")

    return list(entries.items())


@dataclass
class ExtractionPlan:
    archive: Path
    extract_to: Path     # path handed to the backend library's extractall()
    output_dir: Path      # FINAL folder that will hold the extracted content
    unwrap_single_folder: bool
    extracted_dir: Path   # where the archive's own contents actually land the
                          # instant extractall() runs, before any rename --
                          # differs from output_dir only in the single-
                          # internal-folder case below, when that folder's own
                          # name doesn't match the archive's own filename


def plan_extraction(archive_path: Path) -> ExtractionPlan:
    """Decide where an archive's contents should land, per the 3-case rule.

    1. Flat files at the root, or 2. multiple subfolders at the root: wrap
       everything in a new folder named after the archive.
    3. Exactly one folder at the root: extract straight into the archive's
       parent directory, so that folder lands as a sibling with no
       redundant wrapper -- avoids the classic double-nested-folder result.
       Named after the ARCHIVE's OWN filename, not whatever that internal
       folder happened to be called when the archive was originally
       packaged -- an archive's filename is something a user (or an
       automated pipeline) may have deliberately renamed/cleaned up, while
       its internal folder name is just whatever it shipped with and can be
       stale/raw (confirmed live: a real release's own filename had been
       cleaned up, but its one internal folder still carried the original
       raw, uncleaned name -- extracting under THAT name defeated the
       rename entirely). Extraction still necessarily lands at the internal
       folder's own name first (that's what the archive's own stored paths
       dictate), then gets renamed to the archive's filename as a final
       step whenever the two differ.
    """
    entries = _top_level_entries(archive_path)
    parent = archive_path.parent

    if len(entries) == 1 and entries[0][1]:
        extracted_dir = parent / entries[0][0]
        output_dir = parent / archive_path.stem
        return ExtractionPlan(archive_path, parent, output_dir, unwrap_single_folder=True,
                               extracted_dir=extracted_dir)

    output_dir = parent / archive_path.stem
    return ExtractionPlan(archive_path, output_dir, output_dir, unwrap_single_folder=False,
                           extracted_dir=output_dir)


def extract_archive(archive_path) -> Path:
    """Extract archive_path per plan_extraction's rule.

    Returns the resulting output directory. Raises DestinationExistsError
    up front rather than merging into / overwriting an existing folder.
    """
    archive_path = Path(archive_path)
    plan = plan_extraction(archive_path)

    if plan.output_dir.exists():
        raise DestinationExistsError(f"Destination already exists: {plan.output_dir}")

    suffix = archive_path.suffix.lower()
    plan.extract_to.mkdir(parents=True, exist_ok=True)

    try:
        if suffix in (".zip", ".cbz"):
            with zipfile.ZipFile(archive_path) as zf:
                zf.extractall(plan.extract_to)
        elif suffix in (".rar", ".cbr"):
            if not RAR_SUPPORT:
                raise ProcessingError(f"rarfile not installed, cannot extract {archive_path}")
            with rarfile.RarFile(archive_path) as rf:
                rf.extractall(plan.extract_to)
        else:
            raise ProcessingError(f"Unsupported archive type: {archive_path.suffix}")

        if plan.extracted_dir != plan.output_dir:
            plan.extracted_dir.rename(plan.output_dir)
    except Exception:
        # Don't leave a partially-extracted result behind on failure. Safe to
        # remove both unconditionally here: the exists() check above already
        # proved output_dir didn't exist before this call started, and
        # extracted_dir (when different) is a transient intermediate this
        # same call just created.
        shutil.rmtree(plan.output_dir, ignore_errors=True)
        shutil.rmtree(plan.extracted_dir, ignore_errors=True)
        raise

    return plan.output_dir


# ------------------------------------------------------------------ PDF ---
# Ported verbatim from de-comp's pdf_converter.py -- same output-folder
# convention (DestinationExistsError-guarded, named after the source file's
# stem), same renderer/DPI/JPEG-quality choice, so a PDF converted here looks
# exactly like one converted by hand in de-comp.

def convert_pdf(pdf_path, dpi: int = PDF_DPI) -> Path:
    """Render every page of pdf_path to a JPG inside a new folder named
    after the PDF (same convention as extract_archive's flat-file case).

    Returns the resulting output directory. Raises DestinationExistsError
    up front rather than merging into / overwriting an existing folder.
    """
    if not PDF_EXTRACT_SUPPORT:
        raise ProcessingError("pypdfium2 not installed, cannot convert PDF")

    pdf_path = Path(pdf_path)
    output_dir = pdf_path.parent / pdf_path.stem

    if output_dir.exists():
        raise DestinationExistsError(f"Destination already exists: {output_dir}")

    scale = dpi / POINTS_PER_INCH
    output_dir.mkdir(parents=True)

    pdf = pdfium.PdfDocument(pdf_path)
    try:
        page_count = len(pdf)
        pad = max(3, len(str(page_count)))
        for i in range(page_count):
            page = pdf[i]
            try:
                bitmap = page.render(scale=scale)
                try:
                    image = bitmap.to_pil().convert("RGB")
                    image.save(output_dir / f"page_{i + 1:0{pad}d}.jpg", quality=92)
                finally:
                    bitmap.close()
            finally:
                page.close()
    except Exception:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise
    finally:
        pdf.close()

    return output_dir


# -------------------------------------------------------------- unified ---

def extract_file(path) -> Path:
    """Single entry point main.py uses: extracts an archive or converts a
    PDF, whichever `path` is, and returns the resulting folder. Raises
    ProcessingError/DestinationExistsError/OSError on failure -- the caller
    decides what "failure" means for one file in a larger batch."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in ARCHIVE_EXTENSIONS:
        return extract_archive(path)
    if suffix in PDF_EXTENSIONS:
        return convert_pdf(path)
    raise ProcessingError(f"Not an extractable file: {path}")
