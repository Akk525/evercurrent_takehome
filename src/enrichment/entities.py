"""
Entity extraction for candidate events.

Extracts high-value technical entities from thread text using regex and vocabulary
matching. No NLP library required — pattern-based extraction is sufficient and
keeps the system locally runnable.

Entity types:
    part        — component identifiers (SHT40, MX150, STM32, etc.)
    revision    — hardware/software revisions (Rev C, Rev B, v1.2)
    build       — build identifiers (Build B, EVT, DVT, PVT)
    supplier    — supplier/vendor names (Molex, Winbond, Digi-Key, etc.)
    subsystem   — functional subsystem names (BMS, PMIC, USB-C, I2C, etc.)
    deadline    — dates or schedule markers (April 18, EOD, tomorrow)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Entity vocabulary
# ---------------------------------------------------------------------------

# Known part numbers / component identifiers (case-insensitive)
PART_PATTERNS: list[str] = [
    r"SHT\d+",            # Sensirion humidity sensors
    r"MX\d+[A-Z0-9]*",   # Molex connectors, Macronix flash
    r"STM32\w*",          # STMicro MCUs
    r"MAX\d+[A-Z0-9]*",  # Maxim ICs
    r"W25Q\d+[A-Z0-9]*", # Winbond NOR flash
    r"U\d+",              # Reference designators (U7, U12, etc.)
    r"R\d{1,3}\b",        # Resistor designators (R47, R3, etc.)
    r"C\d{1,3}\b",        # Capacitor designators (C12, etc.)
    r"D\d{1,3}\b",        # Diode designators
]

# Hardware/software revision patterns
REVISION_PATTERNS: list[str] = [
    r"\bRev\s+[A-Z]\d*\b",   # Rev C, Rev B2, Rev A (require space; word boundary)
    r"\bv\d+\.\d+(?:\.\d+)?\b",  # v1.2, v2.0.1
    r"\bPCB\s?v?\d\b",        # PCB v2
]

# Build identifiers
BUILD_PATTERNS: list[str] = [
    r"Build\s?[A-Z0-9]+",  # Build B, Build 2
    r"\b(?:EVT|DVT|PVT|MP|EVT\d|DVT\d)\b",  # Standard HW build phases
]

# Supplier / vendor names (exact, case-insensitive)
SUPPLIERS: list[str] = [
    "Molex", "Winbond", "Macronix", "TE Connectivity", "Amphenol",
    "Digi-Key", "DigiKey", "Mouser", "Arrow", "Avnet",
    "STMicro", "STMicroelectronics", "Maxim", "Sensirion",
    "Vishay", "Samsung", "Murata",
]

# Subsystem keywords (case-insensitive full-word match)
SUBSYSTEMS: list[str] = [
    "BMS", "PMIC", "USB-C", "USB PD", "I2C", "SPI", "UART",
    "ADC", "GPIO", "DMA", "bootloader", "firmware",
    "power rail", "power management", "battery charger",
    "thermal pad", "thermal interface",
    "NOR flash", "EEPROM", "SRAM",
]

# Deadline / schedule language (heuristic)
DEADLINE_PATTERNS: list[str] = [
    r"\b(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{1,2}\b",  # Month Day
    r"\bEOD\b",
    r"\bEOW\b",
    r"\bby\s+(?:tomorrow|Monday|Tuesday|Wednesday|Thursday|Friday|end of week)\b",
    r"\bApril\s+\d+\b",  # Specific to mock data date range
]


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

@dataclass
class ExtractedEntities:
    parts: list[str] = field(default_factory=list)
    revisions: list[str] = field(default_factory=list)
    builds: list[str] = field(default_factory=list)
    suppliers: list[str] = field(default_factory=list)
    subsystems: list[str] = field(default_factory=list)
    deadlines: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "parts": self.parts,
            "revisions": self.revisions,
            "builds": self.builds,
            "suppliers": self.suppliers,
            "subsystems": self.subsystems,
            "deadlines": self.deadlines,
        }

    def is_empty(self) -> bool:
        return not any([
            self.parts, self.revisions, self.builds,
            self.suppliers, self.subsystems, self.deadlines,
        ])

    def all_entities(self) -> list[str]:
        """Flat list of all extracted entity strings."""
        out: list[str] = []
        for v in self.to_dict().values():
            out.extend(v)
        return out


def extract_entities(text: str) -> ExtractedEntities:
    """
    Extract structured entities from a text bundle.

    Operates case-insensitively where appropriate.
    Deduplicates within each category.
    """
    result = ExtractedEntities()
    seen: set[str] = set()

    def _add(category: list[str], value: str) -> None:
        normalised = value.strip()
        if normalised and normalised.lower() not in seen:
            seen.add(normalised.lower())
            category.append(normalised)

    # Parts — regex patterns
    for pat in PART_PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            _add(result.parts, m.group(0))

    # Revisions
    for pat in REVISION_PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            _add(result.revisions, m.group(0))

    # Builds
    for pat in BUILD_PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            _add(result.builds, m.group(0))

    # Suppliers — exact name matching
    lower_text = text.lower()
    for supplier in SUPPLIERS:
        if supplier.lower() in lower_text:
            _add(result.suppliers, supplier)

    # Subsystems — word-boundary matching
    for sub in SUBSYSTEMS:
        pattern = r'\b' + re.escape(sub) + r'\b'
        if re.search(pattern, text, re.IGNORECASE):
            _add(result.subsystems, sub)

    # Deadlines
    for pat in DEADLINE_PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            _add(result.deadlines, m.group(0))

    return result
