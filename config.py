# config.py
import os
from pathlib import Path

BASE_DIR   = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
# Folders are created at runtime by lifespan in main.py — not at import time

ALLOWED_EXTENSIONS  = {".xlsx", ".xls", ".csv"}
MAX_FILE_SIZE_MB    = 500
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

FUZZY_MATCH_THRESHOLD = 85
FUZZY_EXACT_THRESHOLD = 95

DATE_FORMAT      = "%m/%d/%Y"
AUTO_DATE_FORMAT = "%m/%d/%Y"

API_TITLE   = "Data Cleaning API"
API_VERSION = "2.0.0"
DEBUG       = os.getenv("DEBUG", "false").lower() == "true"

# ── Output storage layout ──────────────────────────────────────────────────────
DATA_ROOT = BASE_DIR

# logical type (lower-case, used in URLs) -> folder name on disk
DATA_TYPE_FOLDERS: dict[str, str] = {
    "beneficiary":  "beneficiary",
    "banks":        "Banks_Financials",   # shared folder
    "financials":   "Banks_Financials",   # shared folder
    "certificates": "certificates",
}

# Fixed output stem for types that share a folder (no ip_name, no file_id)
FIXED_STEM: dict[str, str] = {
    "banks":      "Banks_Financials",
    "financials": "Banks_Financials",
}

# These types require an ip_name subfolder:
#   <type>/<ip_name>/<ip_name>_cleaned.parquet
#   <type>/<ip_name>/<ip_name>_report.parquet
TYPES_WITH_IP_SUBFOLDER: set[str] = {"beneficiary", "certificates"}

# ── Pakistani Banks ────────────────────────────────────────────────────────────
BANK_NAMES = {
    "National Bank of Pakistan": ["NBP","national bank","natl bank"],
    "Bank of Punjab":            ["BOP","bank of punjab","b.o.p"],
    "Habib Bank Limited":        ["HBL","hbl","habib bank"],
    "MCB Bank Limited":          ["MCB","mcb","muslim commercial bank"],
    "United Bank Limited":       ["UBL","ubl","united bank"],
    "Meezan Bank Limited":       ["meezan","meezan bank","mezan bank"],
    "Allied Bank Limited":       ["ABL","allied bank","allied"],
    "Bank Alfalah Limited":      ["alfalah","bank alfalah","alflah"],
    "Faysal Bank Limited":       ["faysal","faysal bank","faizal bank"],
    "Askari Bank Limited":       ["askari","askari bank","askri bank"],
    "Zarai Taraqiati Bank Limited": ["ZTBL","zarai bank","zarai taraqiati"],
}

BANK_ALIAS_MAP: dict[str, str] = {}
for canonical, aliases in BANK_NAMES.items():
    BANK_ALIAS_MAP[canonical.lower()] = canonical
    for alias in aliases:
        BANK_ALIAS_MAP[alias.lower()] = canonical

# ── Sindh Geography ────────────────────────────────────────────────────────────
SINDH_DISTRICTS = [
    "Badin","Dadu","Ghotki","Hyderabad","Jacobabad","Jamshoro",
    "Karachi Central","Karachi East","Karachi South","Karachi West",
    "Keamari","Korangi","Malir","Kashmore","Khairpur","Larkana",
    "Matiari","Mirpur Khas","Naushahro Feroze","Qambar Shahdadkot",
    "Sanghar","Shaheed Benazirabad","Shikarpur","Sujawal","Sukkur",
    "Tando Allahyar","Tando Muhammad Khan","Tharparkar","Thatta","Umerkot",
]

SINDH_TEHSILS = [
    "Badin","Matli","Talhar","Tando Bago","Dadu","Johi","Mehar",
    "Ghotki","Daharki","Mirpur Mathelo","Ubauro","Hyderabad","Latifabad",
    "Qasimabad","Jacobabad","Garhi Khairo","Thul","Jamshoro","Kotri",
    "Sehwan","Larkana","Dokri","Ratodero","Khairpur","Gambat","Kot Diji",
    "Sanghar","Shahdadpur","Tando Adam","Nawabshah","Sakrand","Shikarpur",
    "Sujawal","Jati","Sukkur","Rohri","Pano Aqil","Tando Allahyar",
    "Tando Muhammad Khan","Mithi","Diplo","Islamkot","Thatta","Umerkot",
    "Kunri","Pithoro","Samaro","Kandhkot","Kashmore","Tangwani",
]

GEO_COLUMNS = {
    "district":      SINDH_DISTRICTS,
    "tehsil":        SINDH_TEHSILS,
    "union_council": SINDH_TEHSILS,
    "taluka":        SINDH_TEHSILS,
    "jh_uc":         SINDH_TEHSILS,
    "jh_deh":        SINDH_TEHSILS,
}
