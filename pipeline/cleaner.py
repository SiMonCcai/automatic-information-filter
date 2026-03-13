"""
HTML content cleaner - converts HTML to plain text.
Matches legacy behavior from tmp_aif/app/api/pull/route.ts:
- Skip img tags
- Ignore href on links (keep anchor text)
- Preserve paragraph/newline structure (do NOT collapse all whitespace)
"""

import html
import logging
import re
from typing import Optional

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

logger = logging.getLogger(__name__)


class HTMLCleaner:
    """Clean HTML content to plain text, matching html-to-text behavior."""

    def __init__(self, preserve_formatting: bool = True):
        self.preserve_formatting = preserve_formatting

    def clean(self, html_content: Optional[str]) -> Optional[str]:
        """
        Convert HTML to plain text.
        Matches behavior: convert(html, {wordwrap: false, selectors: [
            {selector: 'img', format: 'skip'},
            {selector: 'a', options: {ignoreHref: true}}
        ]}).replace(/\n\s*\n/g, '\n')
        """
        if not html_content:
            return None

        if not HAS_BS4:
            logger.warning("BeautifulSoup4 not installed, using basic cleaning")
            return self._basic_clean(html_content)

        try:
            soup = BeautifulSoup(html_content, "html.parser")

            # Remove img tags completely (format: 'skip')
            for img in soup.find_all("img"):
                img.decompose()

            # Remove script, style, noscript elements
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()

            # Strip href from links but keep anchor text
            for a in soup.find_all("a"):
                a.unwrap()  # Remove <a> tag, keep text content

            # Get text preserving line structure
            text = soup.get_text(separator="\n")

            # Decode HTML entities (unescape)
            text = html.unescape(text)

            # Clean up whitespace - preserve paragraphs/newlines
            # Replace consecutive blank lines with single newline
            text = re.sub(r"\n\s*\n", "\n", text)

            # Strip leading/trailing whitespace
            text = text.strip()

            return text

        except Exception as e:
            logger.warning(f"Error cleaning HTML: {e}")
            return self._basic_clean(html_content)

    def _basic_clean(self, html_content: str) -> str:
        """Basic HTML cleaning without BeautifulSoup."""
        # Remove img tags
        text = re.sub(r"<img[^>]*>", "", html_content, flags=re.IGNORECASE)
        # Remove script/style tags
        text = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", "", text, flags=re.IGNORECASE | re.DOTALL)
        # Remove remaining tags
        text = re.sub(r"<[^>]+>", "", text)
        # Decode HTML entities
        text = html.unescape(text)
        # Clean up consecutive blank lines
        text = re.sub(r"\n\s*\n", "\n", text)
        return text.strip()
