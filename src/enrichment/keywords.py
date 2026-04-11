"""
Keyword lists for heuristic signal detection.

These are deliberately explicit — no magic regex soup.
Add or tune these per domain without touching the scoring logic.
"""

# Event type signal words
BLOCKER_KEYWORDS = [
    "blocker", "blocked", "blocking", "hang", "stuck", "fail", "failure",
    "100%", "reproducible", "stops", "cannot proceed", "prevents",
    "locked up", "not booting", "crash", "crashing",
]

DECISION_KEYWORDS = [
    "decision needed", "decision:", "which option", "do we go with", "vote",
    "alignment", "tentative decision", "agreed", "consensus", "we should",
    "recommendation", "proposed", "choose", "choosing",
]

RISK_KEYWORDS = [
    "risk", "at risk", "lead time", "delay", "delayed", "slip", "slipping",
    "qualification", "qual", "failure rate", "out of spec", "spec violation",
    "allocation", "shortage", "14-week", "18-week", "p1", "severity",
]

URGENCY_KEYWORDS = [
    "eod", "asap", "urgent", "immediately", "today", "tomorrow", "by friday",
    "by thursday", "cutoff", "deadline", "before", "critical", "blocker",
    "april 11", "april 12", "april 15", "april 18", "8 days", "build b",
]

NOISE_KEYWORDS = [
    "lunch", "snacks", "brownies", "laughing", "friday lunch", "coffee",
    "birthday", "team lunch", "baozi", "chips and salsa", "fruit",
    "off-topic", "celebration", "happy hour",
]

REQUEST_FOR_INPUT_KEYWORDS = [
    "can you", "could you", "do you have", "please confirm", "flagging to",
    "looping in", "input from", "thoughts?", "any view", "@",
    "can anyone", "does anyone", "have you seen", "did you check",
]

STATUS_UPDATE_KEYWORDS = [
    "update:", "status:", "fyi", "heads up", "preliminary update",
    "in progress", "completed", "finished", "results are in", "just landed",
    "for awareness", "logging", "logged",
]

# Topic domain keywords
TOPIC_MAP: dict[str, list[str]] = {
    "connector": [
        "connector", "mx150", "molex", "amphenol", "te connectivity",
        "pin pitch", "harness", "socket", "header",
    ],
    "pcb": [
        "pcb", "board", "layout", "schematic", "rev c", "rev b",
        "copper pour", "drc", "trace", "via", "pad",
    ],
    "firmware": [
        "firmware", "i2c", "spi", "uart", "bus", "driver", "register",
        "bootloader", "flash", "init", "sequence", "timing", "bringup",
    ],
    "bms": [
        "bms", "battery", "pmic", "fuel gauge", "max17261", "power_good",
        "power management", "charging", "cell",
    ],
    "thermal": [
        "thermal", "temperature", "cycling", "soak", "85c", "40c",
        "heat", "dissipation", "pad", "underfill", "cross-section",
    ],
    "testing": [
        "test", "validation", "qualification", "fail", "failure rate",
        "spec", "measurement", "result", "cycle", "iteration",
    ],
    "supply_chain": [
        "supplier", "lead time", "availability", "stock", "distributor",
        "allocation", "procurement", "bom", "molex", "winbond", "macronix",
        "digi-key", "part number",
    ],
    "sensor": [
        "sensor", "sht40", "imu", "adc", "humidity", "reading", "sample",
        "intermittent", "environmental",
    ],
    "nor_flash": [
        "nor flash", "flash", "w25q128", "winbond", "macronix", "mx25l",
        "storage", "memory",
    ],
}
