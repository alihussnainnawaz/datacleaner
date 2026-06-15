# config.py

import os
from pathlib import Path

# ── Base Paths ────────────────────────────────────────────
BASE_DIR   = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# ── Allowed File Types ────────────────────────────────────
ALLOWED_EXTENSIONS    = {".xlsx", ".xls", ".csv"}
MAX_FILE_SIZE_MB      = 500
MAX_FILE_SIZE_BYTES   = MAX_FILE_SIZE_MB * 1024 * 1024

# ── Fuzzy Matching ────────────────────────────────────────
FUZZY_MATCH_THRESHOLD = 85
FUZZY_EXACT_THRESHOLD = 95

# ── Categorical Columns (spelling/casing checks) ──────────
CATEGORICAL_COLUMNS = [
    "district", "union_council", "tehsil", "province",
    "activity_type", "construction_stage", "gender",
    "ip_name", "bank_name", "bank", "vulnerability_category",
    "grm_status", "funding_source",
]

# ── Standard Allowed Values ───────────────────────────────
ALLOWED_VALUES = {
    "construction_stage": ["Plinth", "Lintel", "Roof"],
    "grm_status":         ["Open", "Under Review", "Resolved"],
    "funding_source":     ["WB", "ADB", "IsDB", "EIB", "GoS", "GoP"],
    "gender":             ["Male", "Female", "Transgender"],
}

# ── Date Format ───────────────────────────────────────────
DATE_FORMAT      = "%m/%d/%Y"
AUTO_DATE_FORMAT = "%m/%d/%Y"   # Auto-pipeline output: numeric month

# ── Binary Column Mapping ─────────────────────────────────
# Applied during auto-clean on detected 0/1 columns
BINARY_MAP = {
    "0":     "No",
    "1":     "Yes",
    "0.0":   "No",
    "1.0":   "Yes",
    "n":     "No",
    "y":     "Yes",
    "no":    "No",
    "yes":   "Yes",
    "false": "No",
    "true":  "Yes",
}

# ── Repeating Digit Pattern ───────────────────────────────
# CNICs or UUIDs like 1111111, 2222222 ... 9999999 are invalid
REPEATING_DIGIT_MIN_LENGTH = 6   # Flag if 6+ same digits in a row

# ── Special Character Categories ─────────────────────────
# Used to assign per-character-type highlight colors on frontend
SPECIAL_CHAR_CATEGORIES = {
    "at":       {"chars": "@",       "label": "At sign"},
    "bang":     {"chars": "!",       "label": "Exclamation"},
    "question": {"chars": "?",       "label": "Question mark"},
    "angle":    {"chars": "<>",      "label": "Angle bracket"},
    "curly":    {"chars": "{}",      "label": "Curly brace"},
    "hash":     {"chars": "#",       "label": "Hash"},
    "dollar":   {"chars": "$",       "label": "Dollar sign"},
    "percent":  {"chars": "%",       "label": "Percent"},
    "caret":    {"chars": "^",       "label": "Caret"},
    "star":     {"chars": "*",       "label": "Asterisk"},
    "pipe":     {"chars": "|\\",     "label": "Pipe/Backslash"},
    "tilde":    {"chars": "~`",      "label": "Tilde/Backtick"},
    "quote":    {"chars": "\"'",     "label": "Quote"},
    "bracket":  {"chars": "[]",      "label": "Square bracket"},
    "plus":     {"chars": "+",       "label": "Plus sign"},
    "equals":   {"chars": "=",       "label": "Equals sign"},
    "semi":     {"chars": ";",       "label": "Semicolon"},
    "colon":    {"chars": ":",       "label": "Colon"},
}

# ── Pakistani Banks ───────────────────────────────────────
# Canonical full name → list of known abbreviations / short forms / variants
# Used for auto-standardization of bank columns

BANK_NAMES = {
    "State Bank of Pakistan": [
        "SBP", "state bank", "statebank",
    ],
    "National Bank of Pakistan": [
        "NBP", "national bank", "natl bank", "national bank pak",
    ],
    "Bank of Punjab": [
        "BOP", "bank of punjab", "b.o.p", "bop bank",
    ],
    "Bank of Khyber": [
        "BOK", "bank of khyber", "b.o.k",
    ],
    "Sindh Bank": [
        "sindh bank", "sindhbank",
    ],
    "First Women Bank Limited": [
        "FWBL", "first women bank", "first women",
    ],
    "Zarai Taraqiati Bank Limited": [
        "ZTBL", "zarai bank", "zarai taraqiati", "agricultural bank",
    ],
    "Allied Bank Limited": [
        "ABL", "allied bank", "allied", "allied bank ltd",
    ],
    "Askari Bank Limited": [
        "askari", "ASKARI", "askari bank", "askri bank", "askari bnk",
    ],
    "Bank Alfalah Limited": [
        "alfalah", "ALFALAH", "bank alfalah", "b. alfalah", "alflah",
    ],
    "Bank Al Habib Limited": [
        "BAHL", "bank al habib", "al habib", "al-habib",
    ],
    "Habib Bank Limited": [
        "HBL", "hbl", "habib bank", "habib bank ltd",
    ],
    "Habib Metropolitan Bank Limited": [
        "HMB", "habib metro", "habib metropolitan", "metro bank",
    ],
    "JS Bank Limited": [
        "JS", "jsbank", "js bank",
    ],
    "MCB Bank Limited": [
        "MCB", "mcb", "muslim commercial bank", "mcb bank",
    ],
    "Soneri Bank Limited": [
        "soneri", "SONERI", "soneri bank",
    ],
    "Standard Chartered Bank Pakistan": [
        "SCB", "scb", "standard chartered", "std chartered",
        "stanchart", "stan chart",
    ],
    "United Bank Limited": [
        "UBL", "ubl", "united bank", "united bank ltd",
    ],
    "Faysal Bank Limited": [
        "faysal", "FAYSAL", "faysal bank", "faysal bnk", "faizal bank",
    ],
    "Meezan Bank Limited": [
        "meezan", "MEEZAN", "meezan bank", "mezan bank", "mezan",
    ],
    "BankIslami Pakistan Limited": [
        "bankislami", "bank islami", "BANKISLAMI", "bank islamic",
    ],
    "Dubai Islamic Bank Pakistan Limited": [
        "DIB", "dib", "dubai islamic", "dubai islamic bank",
    ],
    "Al Baraka Bank (Pakistan) Limited": [
        "albaraka", "al baraka", "AL BARAKA", "baraka bank",
    ],
    "MCB Islamic Bank Limited": [
        "MCB Islamic", "mcb islamic", "mcb-islamic",
    ],
    "Bank Makramah Limited": [
        "makramah", "bank makramah",
    ],
    "Silk Bank Limited": [
        "silk", "silk bank", "SILK",
    ],
    "Citibank N.A. Pakistan": [
        "citi", "citibank", "CITI", "citi bank",
    ],
    "Deutsche Bank AG Pakistan": [
        "deutsche", "deutsch bank", "deutsche bank", "DB",
    ],
    "Industrial and Commercial Bank of China (ICBC) Pakistan": [
        "ICBC", "icbc", "icbc pakistan",
    ],
    "Bank of China Pakistan": [
        "BOC", "bank of china", "boc pakistan",
    ],
    "MUFG Bank Pakistan": [
        "MUFG", "mufg", "mufg bank",
    ],
    "Habib Bank AG Zurich Pakistan": [
        "habib ag", "habib ag zurich", "HABIB AG", "hbz",
    ],
}

# Flat reverse lookup: variant → canonical name
# e.g. "HBL" → "Habib Bank Limited"
BANK_ALIAS_MAP: dict[str, str] = {}
for canonical, aliases in BANK_NAMES.items():
    BANK_ALIAS_MAP[canonical.lower()] = canonical   # canonical maps to itself
    for alias in aliases:
        BANK_ALIAS_MAP[alias.lower()] = canonical


# ── Sindh Geography ───────────────────────────────────────
# Canonical district → list of tehsils/towns → list of UCs
# Used for fuzzy geo standardization

SINDH_DISTRICTS = [
    "Badin", "Dadu", "Ghotki", "Hyderabad", "Jacobabad",
    "Jamshoro", "Karachi Central", "Karachi East", "Karachi South",
    "Karachi West", "Keamari", "Korangi", "Malir", "Kashmore",
    "Khairpur", "Larkana", "Matiari", "Mirpur Khas",
    "Naushahro Feroze", "Qambar Shahdadkot", "Sanghar",
    "Shaheed Benazirabad", "Shikarpur", "Sujawal", "Sukkur",
    "Tando Allahyar", "Tando Muhammad Khan", "Tharparkar",
    "Thatta", "Umerkot",
]

SINDH_TEHSILS = [
    # Badin
    "Badin", "Matli", "Shaheed Fazil Rahu", "Talhar", "Tando Bago",
    # Dadu
    "Dadu", "Johi", "Khairpur Nathan Shah", "Mehar",
    # Ghotki
    "Ghotki", "Daharki", "Khangarh", "Mirpur Mathelo", "Ubauro",
    # Hyderabad
    "Hyderabad City", "Hyderabad", "Latifabad", "Qasimabad", "Rural Hyderabad",
    # Jacobabad
    "Jacobabad", "Garhi Khairo", "Thul",
    # Jamshoro
    "Jamshoro", "Kotri", "Manjhand", "Sehwan", "Thano Bula Khan",
    # Karachi Central
    "Gulberg", "Liaquatabad", "New Karachi", "North Nazimabad", "North Karachi",
    # Karachi East
    "Ferozabad", "Gulshan-e-Iqbal", "Gulzar-e-Hijri", "Jamshed",
    # Karachi South
    "Aram Bagh", "Civil Line", "Garden", "Lyari", "Saddar",
    # Karachi West
    "Baldia", "Mominabad", "Orangi", "SITE",
    # Keamari
    "Baldia Town", "Harbour", "Mauripur", "SITE Town",
    # Korangi
    "Korangi", "Landhi", "Model Colony", "Shah Faisal",
    # Malir
    "Bin Qasim", "Gadap", "Ibrahim Hyderi", "Murad Memon", "Shah Mureed",
    # Kashmore
    "Kandhkot", "Kashmore", "Tangwani",
    # Khairpur
    "Faiz Ganj", "Gambat", "Kingri", "Khairpur", "Kot Diji", "Nara", "Sobhodero",
    # Larkana
    "Bakrani", "Dokri", "Larkana", "Ratodero",
    # Matiari
    "Hala", "Matiari", "Saeedabad",
    # Mirpur Khas
    "Digri", "Hussain Bux Mari", "Jhuddo", "Kot Ghulam Muhammad",
    "Mirpur Khas", "Sindhri",
    # Naushahro Feroze
    "Bhiria", "Kandiaro", "Mehrabpur", "Moro", "Naushahro Feroze",
    # Qambar Shahdadkot
    "Miro Khan", "Nasirabad", "Qambar", "Qubo Saeed Khan",
    "Shahdadkot", "Sijawal Junejo", "Warah",
    # Sanghar
    "Jam Nawaz Ali", "Khipro", "Sanghar", "Shahdadpur",
    "Sinjhoro", "Tando Adam",
    # Shaheed Benazirabad
    "Daulatpur", "Daur", "Nawabshah", "Qazi Ahmed", "Sakrand",
    # Shikarpur
    "Garhi Yasin", "Khanpur", "Lakhi", "Shikarpur",
    # Sujawal
    "Jati", "Mirpur Bathoro", "Shah Bandar", "Sujawal",
    # Sukkur
    "New Sukkur", "Pano Aqil", "Rohri", "Salehpat", "Sukkur",
    # Tando Allahyar
    "Chamber", "Jhando Mari", "Tando Allahyar",
    # Tando Muhammad Khan
    "Bulri Shah Karim", "Tando Ghulam Hyder", "Tando Muhammad Khan",
    # Tharparkar
    "Chachro", "Dahli", "Diplo", "Islamkot", "Kaloi", "Mithi", "Nagarparkar",
    # Thatta
    "Ghorabari", "Keti Bunder", "Mirpur Sakro", "Thatta",
    # Umerkot
    "Kunri", "Pithoro", "Samaro", "Umerkot",
]

# Combined flat list for fuzzy matching — districts + tehsils together
# Frontend/cleaner can query either list depending on the column name
SINDH_ALL_GEO = list(set(SINDH_DISTRICTS + SINDH_TEHSILS))

# Columns that should be checked against Sindh geo lists
GEO_COLUMNS = {
    "district":      SINDH_DISTRICTS,
    "tehsil":        SINDH_TEHSILS,
    "union_council": SINDH_TEHSILS,   # UCs share tehsil names in this dataset
    "taluka":        SINDH_TEHSILS,
    "town":          SINDH_TEHSILS,
}

# ── Bank Columns ──────────────────────────────────────────
# Column names that should be checked against the bank name map
BANK_COLUMNS = ["bank", "bank_name", "bank name", "financial_institution"]

# ── API Settings ──────────────────────────────────────────
API_TITLE   = "Data Transformer API"
API_VERSION = "1.0.0"
DEBUG       = os.getenv("DEBUG", "false").lower() == "true"
