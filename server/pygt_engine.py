"""pyGenomeTracks rendering engine for the PERV Atlas web UI.

This module is a refactor of the original ``generate_and_plot.py`` offline
script. It exposes two public functions:

- ``build_ini_content(spec)``: turn a user-customisable spec dict into the text
  of a ``tracks.ini`` file that pyGenomeTracks can consume.
- ``render(spec, work_dir)``: write the ini, invoke pyGenomeTracks, and return
  the artifact path together with the ini and log text.

All file paths (BigWig, reference BED) are resolved from a server-controlled
whitelist; the front-end only supplies category/filename and parameter values.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent
MULTIOMICS_DIR = BASE_DIR / "new.Multi-omics"
PYGT_REF_DIR = BASE_DIR / "data" / "pygenome_ref"

PERV_STRUCTURE_BED = PYGT_REF_DIR / "PERV_structure.bed"
PERV_LABELS_BED = PYGT_REF_DIR / "PERV_labels.bed"
PERV_GTF = PYGT_REF_DIR / "PERV.gtf"

# Whole-genome annotation tracks (derived from Sus_scrofa.Sscrofa11.1.108.gtf)
# – BED9 for genes, BED12 for transcripts with exon blocks. These cover any
# region of the pig genome, not just PERV loci.
GENOME_GENES_BED = BASE_DIR / "data" / "genome.genes.bed"  # BED9
GENOME_TX_BED = BASE_DIR / "data" / "genome.bed"           # BED12
HOMOLOGOUS_SEQ_BED = BASE_DIR / "data" / "homologous_seq.bed"
HOMOLOGOUS_LOCUS_BED = BASE_DIR / "data" / "homologous_locus.bed"

# Default annotation colours (front-end may override via colors{}).
DEFAULT_ANNOT_COLORS = {
    "gene": "#de77ae",
    "transcript_exon": "#b3cde3",      # exon blocks (pygt color)
    "transcript_arrow": "#fbb4ae",     # direction arrows + backbone
    "homo_seq": "#4a90e2",
    "homo_locus": "#9b59b6",
    "homo_highlight": "#ea580c",   # selected target inside "all" track
    "perv_rltr": "#f4a582",        # 244,165,130
    "perv_coding": "#abdda4",      # 171,221,164
    "perv_lltr": "#92c5de",        # 146,197,222
}

# Legacy aliases used by older call sites / comments.
GENE_TRACK_COLOR = DEFAULT_ANNOT_COLORS["gene"]
TRANSCRIPT_EXON_COLOR = DEFAULT_ANNOT_COLORS["transcript_exon"]
TRANSCRIPT_ARROW_COLOR = DEFAULT_ANNOT_COLORS["transcript_arrow"]
HOMO_SEQ_TRACK_COLOR = DEFAULT_ANNOT_COLORS["homo_seq"]
HOMO_LOCUS_TRACK_COLOR = DEFAULT_ANNOT_COLORS["homo_locus"]

# Built-in PERV_structure.bed itemRgb → component key
_PERV_RGB_TO_KEY = {
    "244,165,130": "perv_rltr",
    "171,221,164": "perv_coding",
    "146,197,222": "perv_lltr",
}

# Region sources that only have start/end (+ strand) — never borrow PERV LTR structure.
HOMO_REGION_SOURCES = frozenset({"homo_seq", "homo_locus"})
ALLOWED_REGION_SOURCES = frozenset({
    "gene", "transcript", "perv", "homo_seq", "homo_locus", "custom", "position", "",
})

PYGENOMETRACKS_BIN = os.environ.get(
    "PYGENOMETRACKS_BIN",
    "/opt/service/miniconda3/envs/rex_env/bin/pyGenomeTracks",
)
# Python interpreter from the same env as pyGenomeTracks (used to run the
# wrapper that injects custom intron/backbone colour properties).
PYGT_PYTHON = os.environ.get(
    "PYGT_PYTHON",
    "/opt/service/miniconda3/envs/rex_env/bin/python",
)
PYGT_WRAPPER = Path(__file__).resolve().parent / "pygt_wrapper.py"

ALLOWED_FORMATS = ("pdf", "svg", "png")
ALLOWED_CATEGORIES = ("ATAC-seq", "ChIP-seq", "RNA-seq", "WGBS")

# pyGenomeTracks visual constants (mirror the original offline script).
PERV_TRACK_HEIGHT_CM = 0.65
PERV_COLLAPSED_YLIM_RANGE = 110.0
INTERVAL_HEIGHT = 90
ROW_SCALE_FACTOR = 2.3
TRACK_SPACER_HEIGHT = 0.12
# Shared UCSC-arrow layout for Homologous Seq/Loci, Genes, and Transcripts.
# Must stay in sync with _ucsc_arrow_block / genes / transcripts INI and
# pygt_wrapper row_scale_factor / bar_height_fraction.
# Track height scales with stacked rows so each arrow keeps this thickness.
HOMO_ARROW_ROW_HEIGHT_CM = 0.40     # desired physical arrow/bar thickness (cm)
HOMO_INTERVAL_HEIGHT = 45           # interval_height in INI
HOMO_ROW_SCALE_FACTOR = 1.25        # denser than pyGT default 2.3
HOMO_BAR_HEIGHT_FRACTION = 0.90     # nearly fill the row (keeps gaps modest)

# Default per-seqtype colours; the front-end may override per-track.
DEFAULT_SEQTYPE_COLORS = {
    "ATAC": "#8dd3c7",
    "H3K27ac": "#bf812d",
    "H3K9ac": "#bc80bd",
    "Pol2": "#a65628",
    "H3K4me1": "#bebada",
    "H3K4me3": "#fb8072",
    "H3K36me3": "#80b1d3",
    "H3K27me3": "#fdb462",
    "H3K9me3": "#b3de69",
    "CTCF": "#80b1d3",
    "RNA": "#fccde5",
    "WGBS": "#d9d9d9",
}

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_REGION_RE = re.compile(r"^[A-Za-z0-9._-]+$")  # validates chrom


def _sanitize_label(value: str, max_len: int = 80) -> str:
    """Strip control characters / newlines so user text can't break the ini."""
    if not value:
        return ""
    cleaned = re.sub(r"[\r\n\t=\[\]]", " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_len]


# ── Errors ───────────────────────────────────────────────────────────────────


class PygtError(ValueError):
    """User-facing validation/render error."""


# ── BigWig path resolution ───────────────────────────────────────────────────


def resolve_bw_path(category: str, filename: str) -> Path:
    """Return absolute Path to a BigWig under new.Multi-omics, or raise.

    Both ``category`` and ``filename`` are user-supplied; we strictly validate
    them against a whitelist before touching the filesystem.
    """
    if category not in ALLOWED_CATEGORIES:
        raise PygtError(f"Invalid category: {category!r}")
    if "/" in filename or ".." in filename or not filename.endswith(".bw"):
        raise PygtError(f"Invalid BigWig filename: {filename!r}")

    p = MULTIOMICS_DIR / category / "data_bw" / filename
    if not p.is_file():
        raise PygtError(f"BigWig not found: {category}/{filename}")
    return p


# ── pig_genes stacking estimate (from generate_and_plot.py) ──────────────────


def _gene_max_rows(
    bed_path: Path, chrom: str, start: int, end: int,
    label_fontsize_bp_per_char: int = 120,
) -> int:
    """Mirror pyGenomeTracks BedTrack stacking to estimate row count."""
    if not bed_path.is_file():
        return 1
    feats: list[tuple[int, int, str]] = []
    with bed_path.open() as fh:
        for line in fh:
            if not line.strip():
                continue
            p = line.rstrip("\n").split("\t")
            if p[0] != chrom:
                continue
            try:
                s, e = int(p[1]), int(p[2])
            except ValueError:
                continue
            if e <= start or s >= end:
                continue
            name = p[3].strip() if len(p) > 3 else ""
            feats.append((s, e, name))
    if not feats:
        return 1
    feats.sort()
    row_last: list[int] = []
    for s, e, name in feats:
        extended = e + (len(name) + 2) * label_fontsize_bp_per_char
        placed = False
        for i, last in enumerate(row_last):
            if last < s:
                row_last[i] = extended
                placed = True
                break
        if not placed:
            row_last.append(extended)
    return max(1, len(row_last))


def _gene_track_height_cm(rows: int) -> float:
    perv_visual = (INTERVAL_HEIGHT / PERV_COLLAPSED_YLIM_RANGE) * PERV_TRACK_HEIGHT_CM
    ylim_range = (rows - 1) * (INTERVAL_HEIGHT * ROW_SCALE_FACTOR) + INTERVAL_HEIGHT
    return perv_visual * (ylim_range / INTERVAL_HEIGHT)


def _homo_track_height_cm(rows: int) -> float:
    """Track height so each stacked arrow has the same physical thickness.

    With our wrapper::

        row_scale = interval_height * HOMO_ROW_SCALE_FACTOR
        ylim_span = interval_height * ((n_rows - 1) * factor + 1)
        bar_data  = interval_height * HOMO_BAR_HEIGHT_FRACTION
        bar_cm    = track_height * (bar_data / ylim_span)

    For bar_cm = HOMO_ARROW_ROW_HEIGHT_CM::

        height = HOMO_ARROW_ROW_HEIGHT_CM * ((n-1)*factor + 1) / frac
    """
    n = max(1, int(rows))
    frac = HOMO_BAR_HEIGHT_FRACTION
    return (
        HOMO_ARROW_ROW_HEIGHT_CM
        * ((n - 1) * HOMO_ROW_SCALE_FACTOR + 1)
        / frac
    )


def _get_out_of_range_features(
    bed_path: Path, chrom: str, start: int, end: int
) -> list[str]:
    """Return unique names of BED features that overlap [start,end] but extend beyond it."""
    out: list[str] = []
    seen: set[str] = set()
    try:
        with open(bed_path) as fh:
            for line in fh:
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.split("\t")
                if len(parts) < 4 or parts[0] != chrom:
                    continue
                try:
                    b_start, b_end = int(parts[1]), int(parts[2])
                except ValueError:
                    continue
                # overlaps the visible region
                if b_start < end and b_end > start:
                    # extends beyond either boundary
                    if b_start < start or b_end > end:
                        name = parts[3].strip()
                        if name and name not in seen:
                            seen.add(name)
                            out.append(name)
    except OSError:
        pass
    return out


def _write_filtered_bed(
    bed_path: Path, chrom: str, start: int, end: int, out_path: Path
) -> None:
    """Write to out_path only features *fully* within [start, end] on chrom.

    Output is sorted by (chrom, start, end) so pyGenomeTracks BedTrack accepts it.
    """
    kept: list[tuple[str, int, int, str]] = []
    with open(bed_path) as fin:
        for line in fin:
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < 3 or parts[0] != chrom:
                continue
            try:
                b_start, b_end = int(parts[1]), int(parts[2])
            except ValueError:
                continue
            if b_start >= start and b_end <= end:
                kept.append((parts[0], b_start, b_end, line if line.endswith("\n") else line + "\n"))
    kept.sort(key=lambda t: (t[0], t[1], t[2]))
    with open(out_path, "w") as fout:
        for _, _, _, line in kept:
            fout.write(line)


# ── Spec validation ──────────────────────────────────────────────────────────


@dataclass
class TrackSpec:
    category: str
    filename: str
    title: str
    color: str
    height_cm: float

    def bw_path(self) -> Path:
        return resolve_bw_path(self.category, self.filename)


@dataclass
class RenderSpec:
    chrom: str
    start: int            # 1-based, inclusive
    end: int              # 1-based, inclusive
    upstream: int
    downstream: int
    tracks: list[TrackSpec]
    annot_perv: bool
    annot_genes: bool
    annot_transcripts: bool
    annot_transcripts_display: str  # "collapsed" or "stacked"
    fontsize: int
    track_label_fraction: float
    number_of_bins: int
    show_data_range: bool
    interval_title: str
    include_partial_genes: bool   # False → filter out genes/txs extending beyond region
    fmt: str              # pdf|svg|png
    region_source: str = ""       # gene|transcript|perv|homo_seq|homo_locus|custom|position
    strand: str = "."             # +|-|. for target-feature arrow tracks
    annot_homo_seq_all: bool = False
    annot_homo_locus_all: bool = False
    colors: dict | None = None    # overrides DEFAULT_ANNOT_COLORS

    @property
    def plot_start(self) -> int:
        return max(1, self.start - self.upstream)

    @property
    def plot_end(self) -> int:
        return self.end + self.downstream

    @property
    def region_str(self) -> str:
        return f"{self.chrom}:{self.plot_start}-{self.plot_end}"

    @property
    def is_homo_source(self) -> bool:
        return self.region_source in HOMO_REGION_SOURCES

    def color(self, key: str) -> str:
        defaults = DEFAULT_ANNOT_COLORS
        if self.colors and key in self.colors:
            return self.colors[key]
        return defaults[key]


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _parse_annot_colors(raw: Any) -> dict[str, str]:
    """Validate optional colors dict; unknown keys ignored, bad values rejected.

    Accepts legacy key ``transcript`` as an alias for ``transcript_exon``.
    """
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise PygtError("colors must be an object")
    out: dict[str, str] = {}
    for key in DEFAULT_ANNOT_COLORS:
        if key not in raw:
            continue
        val = str(raw[key]).strip()
        if not _HEX_RE.match(val):
            raise PygtError(f"Invalid colour for {key!r}: {val!r} (expected #rrggbb)")
        out[key] = val.lower()
    # Legacy alias: older clients sent "transcript" for the exon colour.
    if "transcript_exon" not in out and "transcript" in raw:
        val = str(raw["transcript"]).strip()
        if not _HEX_RE.match(val):
            raise PygtError(
                f"Invalid colour for 'transcript': {val!r} (expected #rrggbb)"
            )
        out["transcript_exon"] = val.lower()
    return out


def _hex_to_rgb_csv(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    return f"{int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)}"


def validate_spec(body: dict[str, Any]) -> RenderSpec:
    """Parse + validate a JSON request body into a RenderSpec."""
    chrom = str(body.get("chrom", "")).strip()
    if not chrom or not _REGION_RE.match(chrom):
        raise PygtError("Invalid or missing chromosome")
    try:
        start = int(body.get("start", 0))
        end = int(body.get("end", 0))
    except (TypeError, ValueError):
        raise PygtError("start/end must be integers")
    if start <= 0 or end <= 0 or end < start:
        raise PygtError("Invalid coordinates")

    upstream = max(0, int(body.get("upstream") or 0))
    downstream = max(0, int(body.get("downstream") or 0))
    span = (end - start + 1) + upstream + downstream
    if span > 10_000_000:
        raise PygtError(f"Region too large ({span:,} bp). Maximum is 10 Mb.")

    raw_tracks = body.get("tracks") or []
    if not raw_tracks:
        raise PygtError("At least one BigWig track must be selected")
    if len(raw_tracks) > 30:
        raise PygtError("Too many tracks (max 30)")

    tracks: list[TrackSpec] = []
    for i, t in enumerate(raw_tracks):
        if not isinstance(t, dict):
            raise PygtError(f"Track #{i+1}: not an object")
        cat = str(t.get("category", "")).strip()
        fname = str(t.get("filename", "")).strip()
        title = _sanitize_label(str(t.get("title") or Path(fname).stem))
        if not title:
            title = f"track_{i+1}"
        color = str(t.get("color", "")).strip() or "#2563eb"
        if not _HEX_RE.match(color):
            raise PygtError(f"Track #{i+1}: invalid colour {color!r} (expected #rrggbb)")
        try:
            h = float(t.get("height_cm", 2.0))
        except (TypeError, ValueError):
            h = 2.0
        h = _clamp(h, 0.5, 8.0)
        # resolve_bw_path performs whitelist + filesystem validation
        spec = TrackSpec(category=cat, filename=fname, title=title, color=color, height_cm=h)
        spec.bw_path()
        tracks.append(spec)

    annot = body.get("annotation") or {}
    options = body.get("options") or {}

    fontsize = int(options.get("fontsize", 12) or 12)
    fontsize = int(_clamp(fontsize, 6, 24))
    label_frac = float(options.get("track_label_fraction", 0.25) or 0.25)
    label_frac = _clamp(label_frac, 0.05, 0.40)
    n_bins = int(options.get("number_of_bins", 700) or 700)
    n_bins = int(_clamp(n_bins, 100, 5000))

    fmt = str(body.get("format", "pdf")).lower()
    if fmt not in ALLOWED_FORMATS:
        raise PygtError(f"Invalid format: {fmt!r}")

    interval_title = _sanitize_label(str(body.get("interval_title", "")))

    region_source = str(body.get("region_source", "")).strip().lower()
    if region_source not in ALLOWED_REGION_SOURCES:
        raise PygtError(
            f"Invalid region_source: {region_source!r}. "
            "Must be one of: gene, transcript, perv, homo_seq, homo_locus, custom, position"
        )
    strand = str(body.get("strand", ".")).strip()
    if strand not in ("+", "-", "."):
        strand = "."

    colors = _parse_annot_colors(body.get("colors"))

    return RenderSpec(
        chrom=chrom,
        start=start,
        end=end,
        upstream=upstream,
        downstream=downstream,
        tracks=tracks,
        annot_perv=bool(annot.get("perv_structure", True)),
        annot_genes=bool(annot.get("genes", True)),
        annot_transcripts=bool(annot.get("transcripts", False)),
        annot_transcripts_display=(
            "stacked" if annot.get("transcripts_display") == "stacked" else "collapsed"
        ),
        fontsize=fontsize,
        track_label_fraction=label_frac,
        number_of_bins=n_bins,
        show_data_range=bool(options.get("show_data_range", True)),
        interval_title=interval_title,
        include_partial_genes=bool(annot.get("include_partial_genes", True)),
        fmt=fmt,
        region_source=region_source,
        strand=strand,
        annot_homo_seq_all=bool(annot.get("homo_seq_all", False)),
        annot_homo_locus_all=bool(annot.get("homo_locus_all", False)),
        colors=colors or None,
    )


# ── INI generation ───────────────────────────────────────────────────────────


_BIGWIG_TEMPLATE = """\
[{section}]
file = {file_path}
title = {title}
height = {height:.2f}
color = {color}
min_value = 0
number_of_bins = {n_bins}
nans_to_zeros = true
summary_method = mean
show_data_range = {show_range}
file_type = bigwig
"""

_SPACER_TEMPLATE = "[spacer]\nheight = {h:.2f}\n"


def _bed_has_overlap(bed_path: Path, chrom: str, start: int, end: int) -> bool:
    """Return True iff bed_path contains any feature overlapping the window."""
    if not bed_path.is_file():
        return False
    try:
        with bed_path.open() as fh:
            for line in fh:
                if not line.strip() or line.startswith("#"):
                    continue
                p = line.rstrip("\n").split("\t")
                if len(p) < 3 or p[0] != chrom:
                    continue
                try:
                    s, e = int(p[1]), int(p[2])
                except ValueError:
                    continue
                if e > start and s < end:
                    return True
    except OSError:
        return False
    return False


def _write_target_feature_bed(spec: RenderSpec, bed_path: Path) -> None:
    """Write a single BED6 feature for the selected homologous/target interval.

    Coordinates on ``RenderSpec`` are 1-based inclusive; BED is 0-based half-open.
    """
    bed_start = max(0, spec.start - 1)
    bed_end = max(bed_start + 1, spec.end)
    name = spec.interval_title or (
        "Homologous Seq" if spec.region_source == "homo_seq" else "Homologous Locus"
    )
    # Sanitize name for BED column 4 (no tabs/newlines).
    name = re.sub(r"[\t\r\n]", " ", name).strip() or "feature"
    strand = spec.strand if spec.strand in ("+", "-") else "."
    bed_path.write_text(
        f"{spec.chrom}\t{bed_start}\t{bed_end}\t{name}\t0\t{strand}\n",
        encoding="utf-8",
    )


def _iter_bed_overlaps(
    bed_path: Path, chrom: str, start: int, end: int,
) -> list[list[str]]:
    """Return BED rows (split fields) overlapping [start, end) on chrom."""
    rows: list[list[str]] = []
    if not bed_path.is_file():
        return rows
    # plot coords are 1-based inclusive; BED is 0-based half-open.
    # Overlap test in BED space: bed_e > (start-1) and bed_s < end
    # Equivalent with 1-based plot window [start, end]: bed_e >= start and bed_s < end
    # Use 0-based window [start-1, end) for clarity.
    win_s = max(0, start - 1) if start >= 1 else start
    win_e = end
    try:
        with bed_path.open() as fh:
            for line in fh:
                if not line.strip() or line.startswith("#"):
                    continue
                p = line.rstrip("\n").split("\t")
                if len(p) < 3 or p[0] != chrom:
                    continue
                try:
                    s, e = int(p[1]), int(p[2])
                except ValueError:
                    continue
                if e > win_s and s < win_e:
                    rows.append(p)
    except OSError:
        return []
    # pyGenomeTracks BedTrack requires sorted BED (chrom, start, end).
    rows.sort(key=lambda p: (p[0], int(p[1]), int(p[2])))
    return rows


def _write_homo_region_bed(
    spec: RenderSpec,
    src_bed: Path,
    out_bed: Path,
    *,
    base_color_key: str,
    highlight_name: str | None,
) -> int:
    """Write overlapping features as BED9 with itemRgb; highlight one by name.

    Returns number of features written.
    """
    rows = _iter_bed_overlaps(src_bed, spec.chrom, spec.plot_start, spec.plot_end)
    if not rows:
        return 0
    base_rgb = _hex_to_rgb_csv(spec.color(base_color_key))
    hi_rgb = _hex_to_rgb_csv(spec.color("homo_highlight"))
    hi_name = (highlight_name or "").strip()
    lines: list[str] = []
    for p in rows:
        name = p[3].strip() if len(p) > 3 else ""
        score = p[4] if len(p) > 4 else "0"
        strand = p[5] if len(p) > 5 and p[5] in ("+", "-") else "."
        s, e = p[1], p[2]
        rgb = hi_rgb if (hi_name and name == hi_name) else base_rgb
        # BED9: chrom start end name score strand thickStart thickEnd itemRgb
        lines.append(f"{p[0]}\t{s}\t{e}\t{name}\t{score}\t{strand}\t{s}\t{e}\t{rgb}")
    out_bed.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def _write_perv_structure_recolored(spec: RenderSpec, out_bed: Path) -> bool:
    """Copy overlapping PERV_structure features with user-selected component colours."""
    rows = _iter_bed_overlaps(
        PERV_STRUCTURE_BED, spec.chrom, spec.plot_start, spec.plot_end,
    )
    if not rows:
        return False
    lines: list[str] = []
    for p in rows:
        # Ensure BED9 width
        while len(p) < 9:
            p.append("")
        old_rgb = p[8].strip() if p[8] else ""
        key = _PERV_RGB_TO_KEY.get(old_rgb)
        if key:
            p[8] = _hex_to_rgb_csv(spec.color(key))
        elif not old_rgb:
            p[8] = _hex_to_rgb_csv(spec.color("perv_coding"))
        # thickStart/thickEnd fallback
        if not p[6]:
            p[6] = p[1]
        if not p[7]:
            p[7] = p[2]
        lines.append("\t".join(p[:9]))
    out_bed.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def _ucsc_arrow_block(
    *,
    section: str,
    bed_path: Path,
    title: str,
    color: str,
    fontsize: int,
    height: float,
    use_bed_rgb: bool = False,
) -> str:
    color_line = "color = bed_rgb" if use_bed_rgb else f"color = {color}"
    return (
        _SPACER_TEMPLATE.format(h=0.30)
        + f"""[{section}]
file = {bed_path}
title = {title}
height = {height:.2f}
{color_line}
arrow_scale = 2.5
bar_height_fraction = {HOMO_BAR_HEIGHT_FRACTION}
row_scale_factor = {HOMO_ROW_SCALE_FACTOR}
labels = true
fontsize = {fontsize}
border color = black
interval_height = {HOMO_INTERVAL_HEIGHT}
style = UCSC
display = stacked
file_type = bed
"""
    )


_LTR_LABEL_REMAP = {
    "lLTR": "5'-LTR",
    "rLTR": "3'-LTR",
}


def _lookup_reference_perv_title(spec: RenderSpec) -> str:
    """Return the reference-genome PERV ID (RF*) overlapping the core interval.

    PERV structure always describes the Sus scrofa reference insertion (RF*),
    never the homologous query ID used for navigation (NH*/HB*/…).
    """
    if not HOMOLOGOUS_SEQ_BED.is_file():
        return ""
    # Prefer overlap with the selected core interval; fall back to plot window.
    core_s = max(0, spec.start - 1) if spec.start >= 1 else spec.start
    core_e = max(core_s + 1, spec.end)
    best_name = ""
    best_ov = 0
    try:
        with HOMOLOGOUS_SEQ_BED.open() as fh:
            for line in fh:
                if not line.strip() or line.startswith("#"):
                    continue
                p = line.rstrip("\n").split("\t")
                if len(p) < 4 or p[0] != spec.chrom:
                    continue
                name = p[3].strip()
                if not name.startswith("RF"):
                    continue
                try:
                    s, e = int(p[1]), int(p[2])
                except ValueError:
                    continue
                ov = min(e, core_e) - max(s, core_s)
                if ov > best_ov:
                    best_ov = ov
                    best_name = name
    except OSError:
        return ""
    return _sanitize_label(best_name) if best_name else ""


def _write_perv_labels_renamed(spec: RenderSpec, out_bed: Path) -> bool:
    """Copy overlapping PERV label anchors, renaming lLTR/rLTR → 5'/3'-LTR."""
    rows = _iter_bed_overlaps(
        PERV_LABELS_BED, spec.chrom, spec.plot_start, spec.plot_end,
    )
    if not rows:
        return False
    lines: list[str] = []
    for p in rows:
        while len(p) < 9:
            p.append("")
        name = p[3].strip() if p[3] else ""
        p[3] = _LTR_LABEL_REMAP.get(name, name)
        lines.append("\t".join(p[:9]))
    out_bed.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def _perv_structure_block(
    spec: RenderSpec, structure_bed: Path, labels_bed: Path,
) -> str:
    """5'-LTR / coding / 3'-LTR structure track (reference PERV only).

    Track title is the reference RF* ID (or ``PERV``). Never reuse a homologous
    sequence / locus navigation ID as this track's title.
    """
    if spec.region_source == "perv" and spec.interval_title:
        title = spec.interval_title
    else:
        title = _lookup_reference_perv_title(spec) or "PERV"
    return (
        _SPACER_TEMPLATE.format(h=0.30)
        + f"""[PERV_structure]
file = {structure_bed}
title = {title}
height = 0.65
color = bed_rgb
labels = false
fontsize = {spec.fontsize}
border color = black
interval_height = 90
style = flybase
display = collapsed
file_type = bed

[PERV_labels]
file = {labels_bed}
overlay previous = yes
height = 0.65
title =
color = #ffffff
labels = true
fontsize = {spec.fontsize}
border color = none
interval_height = 90
style = flybase
display = collapsed
file_type = bed
"""
    )


@dataclass
class _AnnotBeds:
    perv_structure: Path | None = None
    perv_labels: Path | None = None
    homo_seq: Path | None = None
    homo_locus: Path | None = None
    homo_target: Path | None = None  # single selected feature when not merged into "all"


def _prepare_annot_beds(spec: RenderSpec, work_dir: Path) -> _AnnotBeds:
    """Materialise filtered/recolored BED files needed for annotation tracks."""
    beds = _AnnotBeds()

    if spec.annot_perv and PERV_STRUCTURE_BED.is_file() and PERV_LABELS_BED.is_file():
        out = work_dir / "perv_structure_colored.bed"
        labels_out = work_dir / "perv_labels_renamed.bed"
        if _write_perv_structure_recolored(spec, out):
            beds.perv_structure = out
            if _write_perv_labels_renamed(spec, labels_out):
                beds.perv_labels = labels_out
            else:
                # Structure present but no label anchors — still draw bars.
                beds.perv_labels = PERV_LABELS_BED

    # Homologous sequences in region (optional "all" overlay).
    highlight_seq = (
        spec.interval_title if spec.region_source == "homo_seq" else None
    )
    if spec.annot_homo_seq_all and HOMOLOGOUS_SEQ_BED.is_file():
        out = work_dir / "homo_seq_region.bed"
        n = _write_homo_region_bed(
            spec, HOMOLOGOUS_SEQ_BED, out,
            base_color_key="homo_seq",
            highlight_name=highlight_seq,
        )
        if n:
            beds.homo_seq = out

    # Homologous loci in region.
    # When the region source is already a locus, do not recolor the selected
    # feature with homo_highlight (orange): a locus is a single interval and
    # should use the Homologous Loci purple from the colour panel.
    if spec.annot_homo_locus_all and HOMOLOGOUS_LOCUS_BED.is_file():
        out = work_dir / "homo_locus_region.bed"
        n = _write_homo_region_bed(
            spec, HOMOLOGOUS_LOCUS_BED, out,
            base_color_key="homo_locus",
            highlight_name=None,
        )
        if n:
            beds.homo_locus = out

    # Single selected homologous feature when not already merged into the "all" track.
    if spec.region_source == "homo_seq" and beds.homo_seq is None:
        out = work_dir / "homo_target.bed"
        _write_target_feature_bed(spec, out)
        beds.homo_target = out
    elif spec.region_source == "homo_locus" and beds.homo_locus is None:
        out = work_dir / "homo_target.bed"
        _write_target_feature_bed(spec, out)
        beds.homo_target = out

    return beds


def _annotation_block(
    spec: RenderSpec,
    genes_bed: Path | None = None,
    tx_bed: Path | None = None,
    annot_beds: _AnnotBeds | None = None,
) -> str:
    parts: list[str] = []
    ab = annot_beds or _AnnotBeds()

    # PERV structure — available for any region source when features overlap.
    if spec.annot_perv and ab.perv_structure is not None:
        labels_bed = ab.perv_labels or PERV_LABELS_BED
        parts.append(_perv_structure_block(spec, ab.perv_structure, labels_bed))

    # Homologous sequences (all in region, stacked) and/or single selected target.
    # Height scales linearly with stacked rows so each arrow stays the same thickness
    # as Homologous Loci / the single-target track.
    if ab.homo_seq is not None:
        n_rows = _gene_max_rows(
            ab.homo_seq, spec.chrom, spec.plot_start, spec.plot_end,
        )
        parts.append(
            _ucsc_arrow_block(
                section="homo_seq_all",
                bed_path=ab.homo_seq,
                title="Homologous Seq",
                color=spec.color("homo_seq"),
                fontsize=spec.fontsize,
                height=_homo_track_height_cm(n_rows),
                use_bed_rgb=True,
            )
        )
    elif ab.homo_target is not None and spec.region_source == "homo_seq":
        parts.append(
            _ucsc_arrow_block(
                section="homo_target",
                bed_path=ab.homo_target,
                title="Homologous Seq",
                color=spec.color("homo_seq"),
                fontsize=spec.fontsize,
                height=_homo_track_height_cm(1),
            )
        )

    if ab.homo_locus is not None:
        n_rows = _gene_max_rows(
            ab.homo_locus, spec.chrom, spec.plot_start, spec.plot_end,
        )
        parts.append(
            _ucsc_arrow_block(
                section="homo_locus_all",
                bed_path=ab.homo_locus,
                title="Homologous Loci",
                color=spec.color("homo_locus"),
                fontsize=spec.fontsize,
                height=_homo_track_height_cm(n_rows),
                use_bed_rgb=True,
            )
        )
    elif ab.homo_target is not None and spec.region_source == "homo_locus":
        parts.append(
            _ucsc_arrow_block(
                section="homo_target",
                bed_path=ab.homo_target,
                title="Homologous Loci",
                color=spec.color("homo_locus"),
                fontsize=spec.fontsize,
                height=_homo_track_height_cm(1),
            )
        )

    # ── Genes (whole-genome BED9, one row per gene) ──
    # Same fixed-thickness arrow layout as Homologous Seq/Loci.
    _genes_bed = genes_bed or GENOME_GENES_BED
    if spec.annot_genes and _genes_bed.is_file():
        n_rows = _gene_max_rows(
            _genes_bed, spec.chrom, spec.plot_start, spec.plot_end,
        )
        parts.append(
            _ucsc_arrow_block(
                section="genes",
                bed_path=_genes_bed,
                title="Genes",
                color=spec.color("gene"),
                fontsize=spec.fontsize,
                height=_homo_track_height_cm(n_rows),
            )
        )

    # ── Transcripts (whole-genome BED12 with exon blocks) ──
    # collapsed → isoforms of the same gene merge into one row (union of exons),
    #             gene-level labels shown.
    # stacked   → each isoform on its own row, no labels (too cluttered).
    # Height uses the same per-row thickness formula as Genes / Homologous so
    # exon ribbons stay the same physical height regardless of stack depth.
    _tx_bed = tx_bed or GENOME_TX_BED
    if spec.annot_transcripts and _tx_bed.is_file():
        tx_disp = spec.annot_transcripts_display  # "collapsed" or "stacked"
        tx_labels = "true" if tx_disp == "collapsed" else "false"
        if tx_disp == "collapsed":
            # collapsed ≈ one row per gene locus (isoforms merged)
            n_tx_rows = _gene_max_rows(
                (_genes_bed if _genes_bed.is_file() else _tx_bed),
                spec.chrom, spec.plot_start, spec.plot_end,
            )
        else:
            # stacked: each isoform is its own row; labels are off
            n_tx_rows = _gene_max_rows(
                _tx_bed, spec.chrom, spec.plot_start, spec.plot_end,
                label_fontsize_bp_per_char=0,
            )
        tx_height = _homo_track_height_cm(n_tx_rows)
        # Wider spacer between Genes and Transcripts so gene labels don't
        # visually collide with the top of the Transcripts track.
        parts.append(_SPACER_TEMPLATE.format(h=0.50))
        parts.append(
            f"""[transcripts]
file = {_tx_bed}
title = Transcripts
height = {tx_height:.2f}
color = {spec.color("transcript_exon")}
color_arrow = {spec.color("transcript_arrow")}
color_backbone = {spec.color("transcript_arrow")}
arrow_scale = 2.5
bar_height_fraction = {HOMO_BAR_HEIGHT_FRACTION}
row_scale_factor = {HOMO_ROW_SCALE_FACTOR}
labels = {tx_labels}
fontsize = {spec.fontsize}
border color = black
interval_height = {HOMO_INTERVAL_HEIGHT}
style = UCSC
display = {tx_disp}
file_type = bed
"""
        )

    return "\n".join(parts)


def build_ini_content(
    spec: RenderSpec,
    genes_bed: Path | None = None,
    tx_bed: Path | None = None,
    annot_beds: _AnnotBeds | None = None,
) -> str:
    """Compose the full tracks.ini text from a validated RenderSpec."""
    chunks: list[str] = ["[x-axis]\n"]
    chunks.append(_SPACER_TEMPLATE.format(h=TRACK_SPACER_HEIGHT))

    for i, t in enumerate(spec.tracks):
        # Unique section header even if two tracks share a display title
        section = f"track_{i+1}_{re.sub(r'[^A-Za-z0-9_]', '_', t.title)[:40]}"
        chunks.append(
            _BIGWIG_TEMPLATE.format(
                section=section,
                file_path=t.bw_path(),
                title=t.title,
                height=t.height_cm,
                color=t.color,
                n_bins=spec.number_of_bins,
                show_range="true" if spec.show_data_range else "false",
            )
        )
        if i < len(spec.tracks) - 1:
            chunks.append(_SPACER_TEMPLATE.format(h=TRACK_SPACER_HEIGHT))

    chunks.append(_SPACER_TEMPLATE.format(h=TRACK_SPACER_HEIGHT))
    annot = _annotation_block(
        spec, genes_bed=genes_bed, tx_bed=tx_bed, annot_beds=annot_beds,
    )
    if annot:
        chunks.append(annot)

    return "\n".join(chunks).rstrip() + "\n"


# ── Render ───────────────────────────────────────────────────────────────────


@dataclass
class RenderResult:
    artifact_path: Path
    ini_text: str
    log_text: str
    cmd_line: str
    warnings: list[str]   # e.g. gene names extending beyond the plot region


def render(spec: RenderSpec, work_dir: Path, *, timeout_sec: int = 120) -> RenderResult:
    """Write ini, run pyGenomeTracks, return artifact + logs.

    Raises ``PygtError`` on validation/timeout/failure. The work_dir must
    already exist; callers (the API layer) own its lifecycle.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    ini_path = work_dir / "tracks.ini"
    out_path = work_dir / f"out.{spec.fmt}"
    log_path = work_dir / "run.log"

    # ── Detect genes / transcripts that extend beyond the plot region ──────────
    partial_names: list[str] = []
    if spec.annot_genes and GENOME_GENES_BED.is_file():
        partial_names.extend(
            _get_out_of_range_features(
                GENOME_GENES_BED, spec.chrom, spec.plot_start, spec.plot_end
            )
        )
    if spec.annot_transcripts and GENOME_TX_BED.is_file():
        partial_names.extend(
            _get_out_of_range_features(
                GENOME_TX_BED, spec.chrom, spec.plot_start, spec.plot_end
            )
        )
    # de-duplicate while preserving order
    seen_names: set[str] = set()
    out_of_range: list[str] = []
    for n in partial_names:
        if n not in seen_names:
            seen_names.add(n)
            out_of_range.append(n)

    # ── Optionally filter BED files to fully-contained features ───────────────
    genes_bed_path: Path | None = None
    tx_bed_path: Path | None = None
    if not spec.include_partial_genes:
        if spec.annot_genes and GENOME_GENES_BED.is_file():
            genes_bed_path = work_dir / "filtered_genes.bed"
            _write_filtered_bed(
                GENOME_GENES_BED, spec.chrom, spec.plot_start, spec.plot_end,
                genes_bed_path,
            )
        if spec.annot_transcripts and GENOME_TX_BED.is_file():
            tx_bed_path = work_dir / "filtered_tx.bed"
            _write_filtered_bed(
                GENOME_TX_BED, spec.chrom, spec.plot_start, spec.plot_end,
                tx_bed_path,
            )

    annot_beds = _prepare_annot_beds(spec, work_dir)

    ini_text = build_ini_content(
        spec,
        genes_bed=genes_bed_path,
        tx_bed=tx_bed_path,
        annot_beds=annot_beds,
    )
    ini_path.write_text(ini_text, encoding="utf-8")

    if not Path(PYGT_PYTHON).is_file() or not PYGT_WRAPPER.is_file():
        raise PygtError(
            f"pyGenomeTracks wrapper not found "
            f"(python={PYGT_PYTHON}, wrapper={PYGT_WRAPPER}). "
            "Set PYGT_PYTHON env var to point at the env containing pyGenomeTracks."
        )

    cmd = [
        PYGT_PYTHON, str(PYGT_WRAPPER),
        "--tracks", str(ini_path),
        "--region", spec.region_str,
        "--outFileName", str(out_path),
        "--trackLabelFraction", f"{spec.track_label_fraction:.3f}",
        "--fontSize", str(spec.fontsize),
    ]

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_sec,
            universal_newlines=True,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        log_path.write_text(
            f"TIMEOUT after {timeout_sec}s\n\nCMD: {shlex.join(cmd)}\n",
            encoding="utf-8",
        )
        raise PygtError(f"pyGenomeTracks timed out after {timeout_sec}s") from exc

    log_text = proc.stdout or ""
    log_path.write_text(
        f"CMD: {shlex.join(cmd)}\nEXIT: {proc.returncode}\n\n{log_text}",
        encoding="utf-8",
    )

    if proc.returncode != 0 or not out_path.is_file():
        tail = "\n".join(log_text.strip().splitlines()[-20:])
        raise PygtError(
            f"pyGenomeTracks failed (exit={proc.returncode}). Last log lines:\n{tail}"
        )

    return RenderResult(
        artifact_path=out_path,
        ini_text=ini_text,
        log_text=log_text,
        cmd_line=shlex.join(cmd),
        warnings=out_of_range,
    )
