"""Document processing: fill DOCX templates with data."""

import re
from docx import Document


def extract_placeholders(docx_path: str) -> list[str]:
    """Extract all {{...}} placeholders from a DOCX file in order of appearance."""
    ...


def _find_placeholders_in_paragraph(paragraph, pattern, result_list, seen_set):
    """Find placeholders in paragraph text (handling split runs)."""
    ...


def _replace_in_paragraph(paragraph, old_text: str, new_text: str):
    """Replace old_text with new_text in paragraph, handling split runs."""
    ...


def fill_document(template_path: str, output_path: str, replacements: dict[str, str]):
    """Fill a DOCX template with given replacements.

    Args:
        template_path: Path to the template DOCX file.
        output_path: Path to save the filled DOCX file.
        replacements: Dict of {placeholder: value}, e.g. {"{{ФИО}}": "Иванов Иван"}.
    """
    ...
