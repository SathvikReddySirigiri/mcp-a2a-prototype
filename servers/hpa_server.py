"""
MCP server: subcellular localization lookup via the Human Protein Atlas.

Part of the MCP + A2A prototype. Exposes a single tool that returns
subcellular location and tissue specificity for a gene.

Run standalone to smoke-test:
    python servers/hpa_server.py --selftest MSLN
"""

import json
import sys
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

API = "https://www.proteinatlas.org/api/search_download.php"
TIMEOUT = 30.0

# Column specifiers from https://www.proteinatlas.org/about/help/dataaccess
# (g=Gene, pc=Protein class, rnats=RNA tissue specificity,
#  prts=Protein tissue specificity, scl=Subcellular location,
#  secl=Secretome location, scml=Subcellular main location,
#  scal=Subcellular additional location).
COLUMNS = "g,pc,rnats,prts,scl,secl,scml,scal"

mcp = FastMCP("hpa-localization")


def _field_text(row: dict[str, Any], key: str) -> str | None:
    """Join a JSON field that may be a string, list, or null."""
    value = row.get(key)
    if value is None:
        return None
    if isinstance(value, list):
        parts = [str(item).strip() for item in value if item is not None and str(item).strip()]
        return ", ".join(parts) if parts else None
    text = str(value).strip()
    return text or None


def _class_list(row: dict[str, Any]) -> list[str]:
    value = row.get("Protein class")
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _has_annotation(row: dict[str, Any], needle: str) -> bool:
    needle_l = needle.lower()
    for item in _class_list(row):
        if needle_l in item.lower():
            return True
    secretome = row.get("Secretome location")
    if secretome is not None and needle_l in str(secretome).lower():
        return True
    return False


def _locations(row: dict[str, Any]) -> str:
    loc = _field_text(row, "Subcellular location")
    if loc:
        return loc
    parts: list[str] = []
    main = _field_text(row, "Subcellular main location")
    extra = _field_text(row, "Subcellular additional location")
    if main:
        parts.append(main)
    if extra:
        parts.append(extra)
    return ", ".join(parts) if parts else "unavailable"


def _tissue(row: dict[str, Any]) -> str:
    rna = _field_text(row, "RNA tissue specificity")
    protein = _field_text(row, "Protein tissue specificity")
    if rna and protein and rna != protein:
        return f"{rna} (RNA); {protein} (protein)"
    return rna or protein or "unavailable"


def _fetch_rows(gene: str) -> list[Any]:
    """HPA search_download rows for a search term. Raises on HTTP/empty."""
    r = httpx.get(
        API,
        params={
            "search": gene,
            "format": "json",
            "columns": COLUMNS,
            "compress": "no",
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    try:
        data = r.json()
    except json.JSONDecodeError:
        raise LookupError(f"No HPA data for {gene}.") from None
    if not isinstance(data, list) or not data:
        raise LookupError(f"No HPA data for {gene}.")
    return data


def _pick_gene(rows: list[Any], gene: str) -> dict[str, Any]:
    """Exact HUGO symbol match among HPA rows. Raises on unknown."""
    upper = gene.strip().upper()
    for rec in rows:
        if not isinstance(rec, dict):
            continue
        symbol = rec.get("Gene")
        if isinstance(symbol, str) and symbol.upper() == upper:
            return rec
    raise ValueError(f"Unknown gene symbol: {gene}")


@mcp.tool()
def get_localization(gene: str) -> str:
    """Look up subcellular localization and tissue specificity from HPA.

    Answers where a protein is located in the cell, and how
    tissue-restricted it is, using Human Protein Atlas annotations.

    Two limitations bound what this tool can support:

    1. Evidence. HPA subcellular locations are antibody-derived or
       predicted, and confidence varies by gene — this is not uniform
       experimental confirmation.

    2. Accessibility. Membrane annotation indicates a protein is
       membrane-associated, but does not establish that the extracellular
       domain is accessible to a circulating antibody or radioligand
       in vivo.

    Args:
        gene: HUGO gene symbol, e.g. "MSLN", "CEACAM5", "ALB".
    """
    symbol = gene.strip()
    if not symbol:
        return "Unknown gene symbol: (empty)"

    try:
        rows = _fetch_rows(symbol)
        row = _pick_gene(rows, symbol)
    except ValueError as e:
        return str(e)
    except LookupError as e:
        return str(e)
    except httpx.HTTPError as e:
        return f"HPA fetch failed: {e}"

    shown = _field_text(row, "Gene") or symbol
    membrane = "yes" if _has_annotation(row, "membrane") else "no"
    secreted = "yes" if _has_annotation(row, "secreted") else "no"

    return (
        f"{shown} (Human Protein Atlas)\n"
        f"  locations : {_locations(row)}\n"
        f"  tissue    : {_tissue(row)}\n"
        f"  membrane  : {membrane}\n"
        f"  secreted  : {secreted}"
    )


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--selftest":
        print(get_localization(sys.argv[2]))
    else:
        mcp.run(transport="stdio")
