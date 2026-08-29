"""
MCP server: healthy-tissue expression lookup via GTEx.

Part of the MCP + A2A prototype. Exposes a single tool that returns
median mRNA expression for a gene in a GTEx healthy tissue.

Run standalone to smoke-test:
    python servers/gtex_server.py --selftest MSLN
    python servers/gtex_server.py --selftest MSLN Lung
"""

import sys
from functools import lru_cache
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

API = "https://gtexportal.org/api/v2"
TIMEOUT = 30.0
# medianGeneExpression defaults to gtex_v10, which uses GENCODE v39 IDs
# (reference/gene's schema default is v26 / GTEx v8 — do not mix them).
DATASET_ID = "gtex_v10"
GENCODE_VERSION = "v39"

# OpenAPI TissueSiteDetailId enum (GET /api/v2/expression/medianGeneExpression)
TISSUE_IDS: tuple[str, ...] = (
    "Adipose_Subcutaneous",
    "Adipose_Visceral_Omentum",
    "Adrenal_Gland",
    "Artery_Aorta",
    "Artery_Coronary",
    "Artery_Tibial",
    "Bladder",
    "Brain_Amygdala",
    "Brain_Anterior_cingulate_cortex_BA24",
    "Brain_Caudate_basal_ganglia",
    "Brain_Cerebellar_Hemisphere",
    "Brain_Cerebellum",
    "Brain_Cortex",
    "Brain_Frontal_Cortex_BA9",
    "Brain_Hippocampus",
    "Brain_Hypothalamus",
    "Brain_Nucleus_accumbens_basal_ganglia",
    "Brain_Putamen_basal_ganglia",
    "Brain_Spinal_cord_cervical_c-1",
    "Brain_Substantia_nigra",
    "Breast_Mammary_Tissue",
    "Cells_Cultured_fibroblasts",
    "Cells_EBV-transformed_lymphocytes",
    "Cells_Transformed_fibroblasts",
    "Cervix_Ectocervix",
    "Cervix_Endocervix",
    "Colon_Sigmoid",
    "Colon_Transverse",
    "Esophagus_Gastroesophageal_Junction",
    "Esophagus_Mucosa",
    "Esophagus_Muscularis",
    "Fallopian_Tube",
    "Heart_Atrial_Appendage",
    "Heart_Left_Ventricle",
    "Kidney_Cortex",
    "Kidney_Medulla",
    "Liver",
    "Lung",
    "Minor_Salivary_Gland",
    "Muscle_Skeletal",
    "Nerve_Tibial",
    "Ovary",
    "Pancreas",
    "Pituitary",
    "Prostate",
    "Skin_Not_Sun_Exposed_Suprapubic",
    "Skin_Sun_Exposed_Lower_leg",
    "Small_Intestine_Terminal_Ileum",
    "Spleen",
    "Stomach",
    "Testis",
    "Thyroid",
    "Uterus",
    "Vagina",
    "Whole_Blood",
)

mcp = FastMCP("gtex-normal")


def _resolve_tissue(tissue: str) -> str:
    """Map a user tissue string to a TissueSiteDetailId. Raises on unknown."""
    raw = tissue.strip()
    if raw in TISSUE_IDS:
        return raw
    normalized = raw.replace(" ", "_")
    by_lower = {t.lower(): t for t in TISSUE_IDS}
    match = by_lower.get(normalized.lower())
    if match is None:
        raise ValueError(
            f"Unknown tissue '{tissue}'. Available: {', '.join(TISSUE_IDS)}"
        )
    return match


def _resolve_gene(symbol: str) -> dict[str, Any]:
    """HUGO symbol -> GTEx gene record with versioned GENCODE ID. Raises on unknown."""
    r = httpx.get(
        f"{API}/reference/gene",
        params={"geneId": [symbol], "gencodeVersion": GENCODE_VERSION},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json().get("data") or []
    if not data:
        raise ValueError(f"Unknown gene symbol: {symbol}")
    upper = symbol.upper()
    for rec in data:
        if rec.get("geneSymbolUpper") == upper or rec.get("geneSymbol") == symbol:
            return rec
    return data[0]


def _fetch_median(gencode_id: str, tissue_id: str) -> dict[str, Any]:
    """Median gene expression row for one versioned GENCODE ID and tissue."""
    r = httpx.get(
        f"{API}/expression/medianGeneExpression",
        params={
            "gencodeId": [gencode_id],
            "tissueSiteDetailId": [tissue_id],
            "datasetId": DATASET_ID,
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json().get("data") or []
    if not data:
        raise LookupError(f"No expression data for {gencode_id} in {tissue_id}.")
    return data[0]


@lru_cache(maxsize=1)
def _tissue_sample_counts() -> dict[str, int]:
    """Tissue -> RNA-seq sample n for DATASET_ID. Fetched once per process."""
    r = httpx.get(
        f"{API}/dataset/tissueSiteDetail",
        params={"datasetId": DATASET_ID, "itemsPerPage": 100},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    counts: dict[str, int] = {}
    for rec in r.json().get("data") or []:
        tid = rec.get("tissueSiteDetailId")
        total = (rec.get("rnaSeqSampleSummary") or {}).get("totalCount")
        if tid and total is not None:
            counts[tid] = int(total)
    return counts


def _fetch_sample_count(tissue_id: str) -> int | None:
    """RNA-seq sample n for one tissue, served from the cached table."""
    return _tissue_sample_counts().get(tissue_id)


@mcp.tool()
def get_normal_expression(gene: str, tissue: str = "Pancreas") -> str:
    """Look up median expression of a gene in healthy tissue from GTEx.

    Returns the median mRNA expression of a HUGO gene symbol in a GTEx
    healthy tissue, in TPM.

    Two limitations bound what this tool can support:

    1. Units. GTEx TPM values are not directly comparable to cBioPortal
       RSEM values from get_expression, because they use different
       normalization pipelines. The two must not be divided to produce
       a fold change.

    2. Population. GTEx samples come from healthy post-mortem donors, not
       from adjacent-normal tissue in the same patients as any tumor cohort.
       Donor demographics, tissue collection, and batch effects all differ.
       A GTEx-versus-TCGA comparison is therefore directional evidence, not
       a matched tumor-versus-normal analysis.

    Args:
        gene: HUGO gene symbol, e.g. "MSLN", "CEACAM5", "KRAS".
        tissue: GTEx tissue site detail ID, e.g. "Pancreas", "Lung".
            Default is Pancreas.
    """
    try:
        tissue_id = _resolve_tissue(tissue)
    except ValueError as e:
        return str(e)

    try:
        gene_rec = _resolve_gene(gene)
    except ValueError as e:
        return str(e)
    except httpx.HTTPError as e:
        return f"Gene lookup failed: {e}"

    try:
        row = _fetch_median(gene_rec["gencodeId"], tissue_id)
    except LookupError as e:
        return str(e)
    except httpx.HTTPError as e:
        return f"Expression fetch failed: {e}"

    symbol = row.get("geneSymbol") or gene_rec.get("geneSymbol") or gene
    median = row.get("median")
    if median is None:
        return f"No median value returned for {symbol} in {tissue_id}."

    samples_line = "  samples : unavailable"
    try:
        n = _fetch_sample_count(tissue_id)
        if n is not None:
            samples_line = f"  samples : {n}"
    except httpx.HTTPError:
        pass

    unit = row.get("unit") or "TPM"

    return (
        f"{symbol} in GTEx {tissue_id} (healthy donors)\n"
        f"{samples_line}\n"
        f"  median  : {median:.4f}\n"
        f"  units   : {unit}"
    )


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--selftest":
        gene = sys.argv[2]
        tissue = sys.argv[3] if len(sys.argv) > 3 else "Pancreas"
        print(get_normal_expression(gene, tissue))
    else:
        mcp.run(transport="stdio")
    