"""
MCP server: tumor expression lookup via cBioPortal.

Part 1 of the MCP + A2A prototype. Exposes a single tool that returns
mRNA expression statistics for a gene across a TCGA cohort.

Run standalone to smoke-test:
    python servers/gdc_server.py --selftest MSLN
"""

import sys
import statistics
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

API = "https://www.cbioportal.org/api"
TIMEOUT = 30.0

# Cohort shorthand -> (molecular profile id, sample list id)
# Verify these with:
#   curl "https://www.cbioportal.org/api/studies/paad_tcga_pan_can_atlas_2018/molecular-profiles"
COHORTS: dict[str, tuple[str, str]] = {
    "PAAD": (
        "paad_tcga_pan_can_atlas_2018_rna_seq_v2_mrna",
        "paad_tcga_pan_can_atlas_2018_rna_seq_v2_mrna",
    ),
    "LUAD": (
        "luad_tcga_pan_can_atlas_2018_rna_seq_v2_mrna",
        "luad_tcga_pan_can_atlas_2018_rna_seq_v2_mrna",
    ),
    "BRCA": (
        "brca_tcga_pan_can_atlas_2018_rna_seq_v2_mrna",
        "brca_tcga_pan_can_atlas_2018_rna_seq_v2_mrna",
    ),
}

mcp = FastMCP("tcga-expression")


def _resolve_gene(symbol: str) -> dict[str, Any]:
    """HUGO symbol -> cBioPortal gene record. Raises on unknown symbol."""
    r = httpx.get(f"{API}/genes/{symbol.upper()}", timeout=TIMEOUT)
    if r.status_code == 404:
        raise ValueError(f"Unknown gene symbol: {symbol}")
    r.raise_for_status()
    return r.json()


def _fetch_values(entrez_id: int, profile_id: str, sample_list_id: str) -> list[float]:
    """Per-sample expression values for one gene in one cohort."""
    r = httpx.post(
        f"{API}/molecular-profiles/{profile_id}/molecular-data/fetch",
        params={"projection": "SUMMARY"},
        json={"entrezGeneIds": [entrez_id], "sampleListId": sample_list_id},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return [d["value"] for d in r.json() if d.get("value") is not None]


@mcp.tool()
def get_expression(gene: str, cohort: str = "PAAD") -> str:
    """Look up mRNA expression for a gene across a TCGA tumor cohort.

    Returns summary statistics (n, mean, median, quartiles, range) of
    RSEM-normalized expression values across all tumor samples in the cohort.
    Use this whenever asked how highly a gene is expressed in a cancer type,
    or to compare expression levels between genes in the same cohort.

    Args:
        gene: HUGO gene symbol, e.g. "MSLN", "CEACAM5", "KRAS".
        cohort: TCGA cohort code. Supported: PAAD (pancreatic),
                LUAD (lung adenocarcinoma), BRCA (breast).
    """
    cohort = cohort.upper()
    if cohort not in COHORTS:
        return f"Unsupported cohort '{cohort}'. Available: {', '.join(COHORTS)}"

    profile_id, sample_list_id = COHORTS[cohort]

    try:
        gene_rec = _resolve_gene(gene)
    except ValueError as e:
        return str(e)
    except httpx.HTTPError as e:
        return f"Gene lookup failed: {e}"

    try:
        values = _fetch_values(gene_rec["entrezGeneId"], profile_id, sample_list_id)
    except httpx.HTTPError as e:
        return f"Expression fetch failed: {e}"

    if not values:
        return f"No expression data for {gene} in {cohort}."

    values.sort()
    q1, med, q3 = statistics.quantiles(values, n=4)

    return (
        f"{gene_rec['hugoGeneSymbol']} in TCGA-{cohort} (tumor samples only)\n"
        f"  samples : {len(values)}\n"
        f"  mean    : {statistics.mean(values):.1f}\n"
        f"  median  : {med:.1f}\n"
        f"  IQR     : {q1:.1f} - {q3:.1f}\n"
        f"  range   : {values[0]:.1f} - {values[-1]:.1f}\n"
        f"  units   : RSEM normalized counts"
    )


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--selftest":
        print(get_expression(sys.argv[2]))
    else:
        mcp.run(transport="stdio")
