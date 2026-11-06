"""Pièces d'identité : lecture MRZ (ICAO 9303), codes-barres structurés, OCR d'images et de PDF, stockage sécurisé.

Trois sources de données alimentent la même vérification avant enregistrement dans la fiche client (CRM) :

1. **Douchette / lecteur MRZ** (clavier émulé) : ``parse_scanner_input`` accepte le flux brut, en lignes séparées
   ou en une seule ligne continue (88 caractères pour un passeport TD3, 72 pour TD2, 90 pour une CNI TD1),
   ainsi que les codes-barres PDF417 / QR contenant du texte structuré (permis AAMVA, JSON, « CLÉ: valeur »).
2. **Image** (téléversement PNG / JPEG ou photo webcam) : prétraitement Pillow puis Tesseract, d'abord sur la
   zone basse (MRZ, liste blanche ``A-Z0-9<``), ensuite sur la page entière (``fra+ara+eng``).
3. **PDF** : la première page est rendue en image par pypdfium2 (si installé), puis traitée comme une image.

Le moteur OCR est FACULTATIF : ``tesseract_available()`` détecte pytesseract et le binaire ; sans eux, le fichier
est conservé, l'interface le signale et la saisie reste possible (douchette ou manuelle).

Analyseur MRZ écrit en Python pur : formats TD1 (3 × 30, dont la CNI biométrique algérienne), TD2 (2 × 36) et
TD3 (2 × 44) ; toutes les sommes de contrôle (numéro, naissance, expiration, données optionnelles, composite) ;
normalisation des erreurs de lecture (espaces, « « » et autres chevrons pour ``<``, O/0 et I/1 corrigés selon la
position) ; siècle des dates déduit (naissance dans le passé, expiration dans le futur).

Stockage : ``data/media/id_scans/<client>/<nom aléatoire>.<ext>`` (dossier ``_attente`` tant que le client n'est
pas connu), octets magiques vérifiés, taille maximale, images réencodées sans métadonnées (EXIF / GPS), PDF
validés. Les fichiers ne sont jamais servis par ``/static`` : voir ``app/routers/id_scan.py``. Le dossier
``data/media`` est archivé par les sauvegardes et ``restore_scan_files`` le restaure.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import re
import secrets
import shutil
import time
import unicodedata
import warnings
from dataclasses import dataclass, field
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import config
from app.models.booking import Reservation
from app.models.customer import GENDERS, ID_TYPES, Guest, normalize_id_number
from app.models.id_scan import ID_SCAN_SIDES, ID_SCAN_SOURCES, IdScan
from app.models.user import User
from app.services import crm
from app.services.audit_log import log_action
from app.services.common import BusinessError, to_date
from app.services.settings import HotelSettings, now_of

logger = logging.getLogger(__name__)

# ======================================================================== messages (textes sources traduits)
MSG_NO_INPUT = "Aucune donnée reçue du lecteur."
MSG_UNRECOGNIZED = ("Saisie non reconnue : ce n'est ni une MRZ (2 × 44, 2 × 36 ou 3 × 30 caractères) ni un "
                    "code-barres structuré (PDF417 / QR) lisible. Utilisez la photo de la pièce ou la saisie manuelle.")
MSG_MRZ_OK = "MRZ lue : toutes les sommes de contrôle sont valides."
MSG_MRZ_PARTIAL = "MRZ lue avec des sommes de contrôle invalides : vérifiez attentivement chaque champ."
MSG_BARCODE_OK = "Code-barres structuré lu : vérifiez les champs avant de valider."
MSG_TEXT_ONLY = "Aucune MRZ détectée : lecture approximative du texte, vérifiez chaque champ."
MSG_NOTHING_FOUND = "Aucune donnée exploitable n'a été lue sur le document : complétez les champs manuellement."
MSG_NO_TESSERACT = ("Moteur OCR Tesseract non installé : le fichier est conservé, mais la lecture automatique de "
                    "l'image est impossible. Utilisez la douchette MRZ ou saisissez les champs.")
MSG_NO_PDF_ENGINE = ("Lecture automatique des PDF indisponible (module pypdfium2 absent) : le PDF est archivé, "
                     "saisissez les champs.")
MSG_OCR_ERROR = "Erreur du moteur OCR : le fichier est conservé, saisissez les champs."
MSG_ISSUE_DATE = "La date de délivrance ne figure pas dans la MRZ : saisissez-la à partir de la pièce."
MSG_EXPIRED = "Document expiré : vérifiez la validité de la pièce d'identité."
MSG_DOC_CORRECTED = "Numéro de document corrigé automatiquement (confusion de caractères à la lecture)."
MSG_NAME_TRUNCATED = "Nom probablement tronqué dans la MRZ : complétez-le si nécessaire."
MSG_BAD_BIRTH = "Date de naissance illisible dans la MRZ."
MSG_BAD_EXPIRY = "Date d'expiration illisible dans la MRZ."
CHECK_WARNINGS = {
    "document_number": "Somme de contrôle invalide pour le numéro de document.",
    "birth_date": "Somme de contrôle invalide pour la date de naissance.",
    "expiry_date": "Somme de contrôle invalide pour la date d'expiration.",
    "optional_data": "Somme de contrôle invalide pour les données optionnelles.",
    "composite": "Somme de contrôle composite invalide (ligne de contrôle globale).",
}
CHECK_LABELS = {
    "document_number": "Numéro de document",
    "birth_date": "Date de naissance",
    "expiry_date": "Date d'expiration",
    "optional_data": "Données optionnelles",
    "composite": "Contrôle composite",
}

# ======================================================================== nationalités (ISO 3166 alpha-3)
# Libellé français au féminin (« nationalité algérienne »), comme la valeur par défaut des fiches clients.
NATIONALITIES: dict[str, str] = {
    "DZA": "Algérienne", "TUN": "Tunisienne", "MAR": "Marocaine", "LBY": "Libyenne", "MRT": "Mauritanienne",
    "EGY": "Égyptienne", "SDN": "Soudanaise", "ESH": "Sahraouie", "FRA": "Française", "BEL": "Belge",
    "CHE": "Suisse", "LUX": "Luxembourgeoise", "MCO": "Monégasque", "ESP": "Espagnole", "PRT": "Portugaise",
    "ITA": "Italienne", "DEU": "Allemande", "D": "Allemande", "AUT": "Autrichienne", "NLD": "Néerlandaise",
    "GBR": "Britannique", "GBD": "Britannique", "GBN": "Britannique", "GBO": "Britannique", "IRL": "Irlandaise",
    "DNK": "Danoise", "SWE": "Suédoise", "NOR": "Norvégienne", "FIN": "Finlandaise", "ISL": "Islandaise",
    "POL": "Polonaise", "CZE": "Tchèque", "SVK": "Slovaque", "HUN": "Hongroise", "ROU": "Roumaine",
    "BGR": "Bulgare", "GRC": "Grecque", "CYP": "Chypriote", "MLT": "Maltaise", "HRV": "Croate", "SVN": "Slovène",
    "SRB": "Serbe", "BIH": "Bosnienne", "MNE": "Monténégrine", "ALB": "Albanaise", "MKD": "Macédonienne",
    "UKR": "Ukrainienne", "RUS": "Russe", "BLR": "Biélorusse", "TUR": "Turque", "AZE": "Azerbaïdjanaise",
    "GEO": "Géorgienne", "ARM": "Arménienne", "KAZ": "Kazakhe", "UZB": "Ouzbèke",
    "SAU": "Saoudienne", "ARE": "Émirienne", "QAT": "Qatarienne", "KWT": "Koweïtienne", "BHR": "Bahreïnienne",
    "OMN": "Omanaise", "YEM": "Yéménite", "JOR": "Jordanienne", "LBN": "Libanaise", "SYR": "Syrienne",
    "IRQ": "Irakienne", "PSE": "Palestinienne", "IRN": "Iranienne", "AFG": "Afghane", "PAK": "Pakistanaise",
    "IND": "Indienne", "BGD": "Bangladaise", "CHN": "Chinoise", "JPN": "Japonaise", "KOR": "Sud-coréenne",
    "VNM": "Vietnamienne", "THA": "Thaïlandaise", "MYS": "Malaisienne", "IDN": "Indonésienne", "PHL": "Philippine",
    "MLI": "Malienne", "NER": "Nigérienne", "TCD": "Tchadienne", "SEN": "Sénégalaise", "GMB": "Gambienne",
    "GIN": "Guinéenne", "CIV": "Ivoirienne", "BFA": "Burkinabè", "GHA": "Ghanéenne", "TGO": "Togolaise",
    "BEN": "Béninoise", "NGA": "Nigériane", "CMR": "Camerounaise", "GAB": "Gabonaise", "COG": "Congolaise",
    "COD": "Congolaise (RDC)", "CAF": "Centrafricaine", "ETH": "Éthiopienne", "SOM": "Somalienne",
    "KEN": "Kényane", "ZAF": "Sud-africaine", "MDG": "Malgache", "COM": "Comorienne", "DJI": "Djiboutienne",
    "USA": "Américaine", "CAN": "Canadienne", "MEX": "Mexicaine", "BRA": "Brésilienne", "ARG": "Argentine",
    "CHL": "Chilienne", "COL": "Colombienne", "VEN": "Vénézuélienne", "CUB": "Cubaine", "AUS": "Australienne",
    "NZL": "Néo-zélandaise",
    "XXA": "Apatride", "XXB": "Réfugiée", "XXC": "Réfugiée", "XXX": "Non précisée",
    "UNO": "Organisation des Nations unies", "UNA": "Organisation des Nations unies",
}


def _fold(text: str) -> str:
    """Majuscules sans accents ni espaces superflus (comparaisons tolérantes)."""
    value = unicodedata.normalize("NFKD", text or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return " ".join(value.upper().split())


_NATIONALITY_BY_LABEL = {_fold(label): label for label in NATIONALITIES.values()}
_NATIONALITY_BY_LABEL.update({"ALGERIEN": "Algérienne", "ALGERIE": "Algérienne", "ALGERIA": "Algérienne",
                              "ALGERIAN": "Algérienne", "FRANCAIS": "Française", "FRENCH": "Française",
                              "TUNISIEN": "Tunisienne", "MAROCAIN": "Marocaine", "جزائرية": "Algérienne",
                              "جزائري": "Algérienne"})


def nationality_label(code: str) -> str:
    """Code ICAO / ISO 3166 alpha-3 → libellé français ; repli sur le code lui-même s'il est inconnu."""
    clean = (code or "").replace("<", "").strip().upper()
    return NATIONALITIES.get(clean, clean)


def _nationality_from_text(value: str) -> str:
    clean = (value or "").strip()
    if not clean:
        return ""
    if re.fullmatch(r"[A-Za-z]{3}", clean) and clean.upper() in NATIONALITIES:
        return NATIONALITIES[clean.upper()]
    return _NATIONALITY_BY_LABEL.get(_fold(clean), clean[:80])


# ======================================================================== résultat d'extraction
FIELD_KEYS = ("last_name", "first_name", "last_name_ar", "first_name_ar", "birth_date", "birth_place", "gender",
              "nationality", "id_type", "id_number", "id_expiry_date", "id_issue_date", "id_issue_place")


@dataclass
class ExtractionResult:
    """Résultat d'une lecture : champs proposés (format des fiches clients), contrôles et avertissements."""

    fields: dict[str, str] = field(default_factory=dict)
    extra: dict[str, str] = field(default_factory=dict)       # code nationalité, État émetteur, n° personnel…
    format: str = ""                                          # td1 | td2 | td3 | aamva | json | cles | texte
    engine: str = "none"                                      # mrz | tesseract | none
    mrz_lines: list[str] = field(default_factory=list)
    raw_text: str = ""
    checks: dict[str, bool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    confidence: float = 0.0
    message: str = ""

    @property
    def mrz(self) -> str:
        return "\n".join(self.mrz_lines)

    @property
    def has_data(self) -> bool:
        return any(self.fields.get(key) for key in ("last_name", "first_name", "id_number", "birth_date"))

    @property
    def checksums_valid(self) -> bool:
        return bool(self.checks) and all(self.checks.values())

    @property
    def status(self) -> str:
        if not self.has_data:
            return "failed"
        if self.format in ("td1", "td2", "td3"):
            return "extracted" if self.checksums_valid else "partial"
        if self.format in ("aamva", "json", "cles"):
            return "extracted" if self.fields.get("id_number") and self.fields.get("last_name") else "partial"
        return "partial"

    def warn(self, message: str) -> None:
        if message and message not in self.warnings:
            self.warnings.append(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fields": {key: self.fields.get(key, "") for key in FIELD_KEYS},
            "extra": dict(self.extra), "format": self.format, "engine": self.engine, "mrz": self.mrz,
            "checks": dict(self.checks), "checksums_valid": self.checksums_valid, "warnings": list(self.warnings),
            "notes": list(self.notes), "confidence": round(self.confidence, 2), "status": self.status,
            "message": self.message,
        }


# ======================================================================== MRZ : sommes de contrôle
MRZ_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<")
_WEIGHTS = (7, 3, 1)


def _char_value(ch: str) -> int:
    if ch.isdigit():
        return int(ch)
    if "A" <= ch <= "Z":
        return ord(ch) - 55
    return 0  # « < » (remplissage)


def check_digit(data: str) -> str:
    """Chiffre de contrôle ICAO 9303 : pondération 7-3-1, A=10 … Z=35, « < » = 0, modulo 10."""
    return str(sum(_char_value(ch) * _WEIGHTS[i % 3] for i, ch in enumerate(data)) % 10)


def check_ok(data: str, digit: str, optional: bool = False) -> bool:
    """Contrôle d'un champ ; pour un champ facultatif vide, le chiffre « < » vaut 0."""
    if digit == "<":
        return optional and set(data) <= {"<"}
    return digit.isdigit() and check_digit(data) == digit


# ======================================================================== MRZ : normalisation
_LOOKALIKES = {
    "«": "<", "‹": "<", "〈": "<", "＜": "<", "˂": "<", "﹤": "<", "⟨": "<", "❮": "<", "{": "<", "[": "<",
    "(": "<", "≪": "<<", "»": "<", "›": "<",
}
# Rangée des chiffres d'un clavier AZERTY tapée par une douchette configurée en QWERTY (sans Maj).
_AZERTY_DIGITS = str.maketrans({"&": "1", "é": "2", '"': "3", "'": "4", "-": "6", "è": "7", "_": "8",
                                "ç": "9", "à": "0"})
TO_DIGIT = str.maketrans({"O": "0", "Q": "0", "D": "0", "U": "0", "I": "1", "L": "1", "Z": "2", "S": "5",
                          "B": "8", "G": "6", "T": "7"})
TO_ALPHA = str.maketrans({"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B", "6": "G", "4": "A", "7": "T"})
# Substitutions essayées une à une sur le numéro de document quand sa somme de contrôle échoue.
_AMBIGUOUS = {"0": "OD", "O": "0", "D": "0", "Q": "0", "1": "I", "I": "1", "L": "1", "5": "S", "S": "5",
              "8": "B", "B": "8", "2": "Z", "Z": "2", "6": "G", "G": "6"}

FORMATS = {"td1": (3, 30), "td2": (2, 36), "td3": (2, 44)}


def _clean_line(line: str) -> str:
    value = unicodedata.normalize("NFKC", line)
    for bad, good in _LOOKALIKES.items():
        value = value.replace(bad, good)
    value = value.upper()
    return "".join(ch for ch in value if ch in MRZ_ALPHABET)


def _prepare_text(text: str) -> str:
    value = text or ""
    if not re.search(r"\d", value) and re.search(r"[&é\"'(\-è_çà]", value):
        value = value.translate(_AZERTY_DIGITS)
    return value


def _fit(line: str, length: int) -> str:
    return (line + "<" * length)[:length]


def _schema(fmt: str) -> list[str]:
    """Nature de chaque position : n = numérique, a = alphabétique, x = alphanumérique (pas de correction)."""
    if fmt == "td3":
        return ["a" * 44, "x" * 9 + "n" + "aaa" + "n" * 7 + "a" + "n" * 7 + "x" * 14 + "nn"]
    if fmt == "td2":
        return ["a" * 36, "x" * 9 + "n" + "aaa" + "n" * 7 + "a" + "n" * 7 + "x" * 7 + "n"]
    return ["aaaaa" + "x" * 9 + "n" + "x" * 15, "n" * 7 + "a" + "n" * 7 + "aaa" + "x" * 11 + "n", "a" * 30]


def _correct(lines: list[str], fmt: str) -> list[str]:
    """Corrige O/0, I/1, S/5… selon la nature de chaque position (zones numériques / alphabétiques)."""
    fixed = []
    for line, kinds in zip(lines, _schema(fmt)):
        chars = []
        for ch, kind in zip(line, kinds):
            if ch == "<":
                chars.append(ch)
            elif kind == "n":
                chars.append(ch.translate(TO_DIGIT))
            elif kind == "a":
                chars.append(ch.translate(TO_ALPHA))
            else:
                chars.append(ch)
        fixed.append("".join(chars))
    return fixed


def _check_score(lines: list[str], fmt: str) -> int:
    """Nombre de sommes de contrôle valides (sert à choisir la meilleure interprétation)."""
    try:
        return sum(1 for ok in _checks(lines, fmt)[0].values() if ok)
    except (IndexError, ValueError):
        return 0


def _candidates(lines: list[str]) -> list[tuple[str, list[str], int]]:
    """Interprétations possibles (format, lignes ajustées, écart de longueur total)."""
    found = []
    for fmt, (count, length) in FORMATS.items():
        for start in range(0, max(0, len(lines) - count + 1)):
            group = lines[start:start + count]
            gaps = [abs(len(line) - length) for line in group]
            if all(len(line) >= length - 6 and len(line) <= length + 3 for line in group) and "<" in "".join(group):
                found.append((fmt, [_fit(line, length) for line in group], sum(gaps)))
    joined = "".join(lines)
    for fmt, (count, length) in FORMATS.items():
        total = count * length
        if abs(len(joined) - total) <= 2 and "<" in joined:
            fitted = _fit(joined, total)
            found.append((fmt, [fitted[i * length:(i + 1) * length] for i in range(count)], abs(len(joined) - total)))
    return found


def _plausible(fmt: str, lines: list[str]) -> bool:
    first = lines[0]
    if not first or first[0] not in "PIACVD":
        return False
    if fmt == "td1":
        return lines[1][:6].translate(TO_DIGIT).isdigit() or lines[1][8:14].translate(TO_DIGIT).isdigit()
    return lines[1][13:19].translate(TO_DIGIT).isdigit() or lines[1][21:27].translate(TO_DIGIT).isdigit()


def find_mrz(text: str) -> tuple[str, list[str]] | None:
    """Repère une MRZ dans un texte (douchette, OCR) ; renvoie (format, lignes corrigées) ou None."""
    raw_lines = re.split(r"[\r\n\x1e\x1d]+", _prepare_text(text))
    lines = [line for line in (_clean_line(item) for item in raw_lines) if line]
    if not lines:
        return None
    # Bruit OCR : ne garder que les lignes qui ressemblent à une ligne MRZ (assez longues).
    mrz_like = [line for line in lines if len(line) >= 24]
    best: tuple[int, int, str, list[str]] | None = None
    for source in (mrz_like, lines):
        for fmt, group, gap in _candidates(source):
            corrected = _correct(group, fmt)
            if not _plausible(fmt, corrected):
                continue
            score = _check_score(corrected, fmt)
            key = (score, -gap)
            if best is None or key > (best[0], best[1]):
                best = (score, -gap, fmt, corrected)
        if best is not None:
            break
    if best is None:
        return None
    return best[2], best[3]


# ======================================================================== MRZ : analyse
def mrz_date(value: str, kind: str, today: date) -> date | None:
    """Date AAMMJJ ; siècle déduit : naissance jamais dans le futur, expiration dans les 50 ans à venir."""
    if not re.fullmatch(r"\d{6}", value or ""):
        return None
    yy, mm, dd = int(value[:2]), int(value[2:4]), int(value[4:6])
    year = 2000 + yy
    try:
        if kind == "birth":
            if date(year, mm, dd) > today:
                year -= 100
        elif year > today.year + 50:
            year -= 100
        return date(year, mm, dd)
    except ValueError:
        return None


def _names(field_value: str) -> tuple[str, str, bool]:
    """« NOM<<PRENOM<SECOND » → (nom, prénoms, tronqué ?) ; séparateur « << » entre nom et prénoms.

    Un champ rempli jusqu'au bout (sans « < » final) signale un nom tronqué par l'émetteur. L'OCR lit parfois
    le remplissage « <<<< » comme « KKKK » : une suite finale mêlant « < » et au moins trois « K » est retirée.
    """
    truncated = bool(field_value) and not field_value.endswith("<")
    value = field_value.rstrip("<")
    tail = re.search(r"(?:<+K+|K{3,})[<K]*$", value)
    if tail:
        run = field_value[tail.start():]
        if run.count("K") >= 4 or (run.count("K") >= 3 and "<" in run):
            value = value[:tail.start()].rstrip("<")
            truncated = False
    surname, _, given = value.partition("<<")
    clean = lambda part: " ".join(p.capitalize() for p in part.replace("<", " ").split() if p)  # noqa: E731
    return clean(surname), clean(given), truncated


def _sex(value: str) -> str:
    return {"M": "M", "F": "F", "H": "M"}.get(value, "")


def document_type(code: str) -> str:
    """Code de document MRZ → type de pièce des fiches clients."""
    code = (code or "").replace("<", "")
    if code.startswith("P"):
        return "passport"
    if code[:1] in ("I", "A", "C") or code == "ID":
        return "cni"
    if code.startswith("D"):
        return "permis"
    return "autre"


def _td1_number(line1: str) -> tuple[str, str, str]:
    """(numéro, données couvertes par le contrôle, chiffre) ; numéro long : suite dans les données optionnelles."""
    number, digit = line1[5:14], line1[14]
    if digit == "<" and line1[15:30].strip("<"):
        optional = line1[15:30]
        end = optional.find("<")
        extension = optional if end == -1 else optional[:end]
        if len(extension) >= 2:
            return number + extension[:-1], number + extension[:-1], extension[-1]
    return number, number, digit


def _checks(lines: list[str], fmt: str) -> tuple[dict[str, bool], dict[str, str]]:
    """Contrôles d'une MRZ corrigée et champs bruts (numéro, dates, sexe, nationalité, optionnels)."""
    if fmt in ("td3", "td2"):
        l1, l2 = lines
        raw = {"code": l1[0:2], "state": l1[2:5], "names": l1[5:], "number": l2[0:9], "nationality": l2[10:13],
               "birth": l2[13:19], "sex": l2[20], "expiry": l2[21:27]}
        checks = {
            "document_number": check_ok(l2[0:9], l2[9]),
            "birth_date": check_ok(l2[13:19], l2[19]),
            "expiry_date": check_ok(l2[21:27], l2[27]),
        }
        if fmt == "td3":
            raw["optional"] = l2[28:42]
            checks["optional_data"] = check_ok(l2[28:42], l2[42], optional=True)
            checks["composite"] = check_ok(l2[0:10] + l2[13:20] + l2[21:43], l2[43])
        else:
            raw["optional"] = l2[28:35]
            checks["composite"] = check_ok(l2[0:10] + l2[13:20] + l2[21:35], l2[35])
        return checks, raw
    l1, l2, l3 = lines
    number, covered, digit = _td1_number(l1)
    raw = {"code": l1[0:2], "state": l1[2:5], "names": l3, "number": number, "nationality": l2[15:18],
           "birth": l2[0:6], "sex": l2[7], "expiry": l2[8:14],
           "optional": (l1[15:30] if len(number) <= 9 else l1[15 + len(number) - 8:30]) + "<" + l2[18:29]}
    checks = {
        "document_number": check_ok(covered, digit),
        "birth_date": check_ok(l2[0:6], l2[6]),
        "expiry_date": check_ok(l2[8:14], l2[14]),
        "composite": check_ok(l1[5:30] + l2[0:7] + l2[8:15] + l2[18:29], l2[29]),
    }
    return checks, raw


def _repair_number(lines: list[str], fmt: str) -> list[str] | None:
    """Essaie les confusions courantes (O/0, I/1, S/5…) sur le numéro de document pour valider sa somme."""
    row, start, end, digit_pos = (0, 5, 14, 14) if fmt == "td1" else (1, 0, 9, 9)
    line = lines[row]
    if line[digit_pos] == "<":
        return None
    for index in range(start, end):
        for replacement in _AMBIGUOUS.get(line[index], ""):
            candidate = line[:index] + replacement + line[index + 1:]
            if check_ok(candidate[start:end], candidate[digit_pos]):
                repaired = list(lines)
                repaired[row] = candidate
                return repaired
    return None


def parse_mrz_lines(fmt: str, lines: list[str], today: date | None = None) -> ExtractionResult:
    """Analyse une MRZ déjà repérée (``find_mrz``) : champs, contrôles, avertissements."""
    today = today or date.today()
    result = ExtractionResult(format=fmt, engine="mrz")
    checks, raw = _checks(lines, fmt)
    if not checks["document_number"]:
        repaired = _repair_number(lines, fmt)
        if repaired is not None:
            lines = repaired
            checks, raw = _checks(lines, fmt)
            result.warn(MSG_DOC_CORRECTED)
    result.mrz_lines = list(lines)
    result.checks = checks
    surname, given, truncated = _names(raw["names"])
    birth = mrz_date(raw["birth"], "birth", today)
    expiry = mrz_date(raw["expiry"], "expiry", today)
    nationality_code = raw["nationality"].replace("<", "")
    result.fields = {
        "last_name": surname, "first_name": given,
        "birth_date": birth.isoformat() if birth else "",
        "gender": _sex(raw["sex"]),
        "nationality": nationality_label(nationality_code),
        "id_type": document_type(raw["code"]),
        "id_number": raw["number"].replace("<", ""),
        "id_expiry_date": expiry.isoformat() if expiry else "",
        "id_issue_date": "", "id_issue_place": "",
    }
    result.extra = {
        "document_code": raw["code"].replace("<", ""), "issuing_state": raw["state"].replace("<", ""),
        "issuing_state_label": nationality_label(raw["state"]), "nationality_code": nationality_code,
        "optional_data": " ".join(part for part in raw["optional"].split("<") if part),
    }
    for name, ok in checks.items():
        if not ok:
            result.warn(CHECK_WARNINGS[name])
    if birth is None:
        result.warn(MSG_BAD_BIRTH)
    if expiry is None:
        result.warn(MSG_BAD_EXPIRY)
    elif expiry < today:
        result.warn(MSG_EXPIRED)
    if truncated:
        result.warn(MSG_NAME_TRUNCATED)
    result.notes.append(MSG_ISSUE_DATE)
    valid = sum(1 for ok in checks.values() if ok)
    result.confidence = round(0.4 + 0.6 * valid / len(checks), 2)
    result.message = MSG_MRZ_OK if result.checksums_valid else MSG_MRZ_PARTIAL
    return result


def parse_mrz(text: str, today: date | None = None) -> ExtractionResult | None:
    """Repère et analyse une MRZ dans un texte quelconque ; None si aucune MRZ n'est trouvée."""
    found = find_mrz(text)
    if found is None:
        return None
    result = parse_mrz_lines(found[0], found[1], today)
    result.raw_text = text or ""
    return result


# ======================================================================== codes-barres structurés (PDF417 / QR)
_KEY_ALIASES = {
    "last_name": ("nom", "nom de famille", "last name", "lastname", "surname", "family name", "familyname", "name",
                  "اللقب"),
    "first_name": ("prenom", "prenoms", "first name", "firstname", "given name", "given names", "givenname",
                   "givennames", "الاسم"),
    "birth_date": ("date de naissance", "date naissance", "naissance", "ne le", "nee le", "birth date",
                   "birthdate", "date of birth", "dob", "تاريخ الميلاد", "تاريخ الازدياد"),
    "birth_place": ("lieu de naissance", "lieu naissance", "place of birth", "birthplace", "birth place",
                    "مكان الميلاد", "مكان الازدياد"),
    "gender": ("sexe", "sex", "gender", "الجنس"),
    "nationality": ("nationalite", "nationality", "الجنسية"),
    "id_number": ("numero", "num", "no", "n", "numero du document", "numero de document", "numero document",
                  "document number", "documentnumber", "doc number", "id number", "idnumber", "license number",
                  "licence number", "numero de permis", "numero de carte", "card number", "رقم", "الرقم"),
    "id_type": ("type", "type de document", "document type", "doctype", "type de piece"),
    "id_expiry_date": ("date d expiration", "date expiration", "expiration", "expire le", "valable jusqu au",
                       "date de fin de validite", "fin de validite", "expiry", "expiry date", "date of expiry",
                       "expiration date", "valid until", "تاريخ الانتهاء", "صالحة الى"),
    "id_issue_date": ("date de delivrance", "date delivrance", "delivre le", "delivree le", "issue date",
                      "date of issue", "issued", "تاريخ الإصدار", "تاريخ الاصدار"),
    "id_issue_place": ("lieu de delivrance", "delivre a", "delivree a", "autorite", "issuing authority",
                       "place of issue", "مكان الإصدار"),
}
_ALIAS_INDEX = {_fold(alias).replace("_", " ").replace("'", " "): key
                for key, aliases in _KEY_ALIASES.items() for alias in aliases}


def _key_of(label: str) -> str | None:
    folded = _fold(re.sub(r"[_'’.°º#-]+", " ", label or "")).strip(" :")
    folded = re.sub(r"\s+", " ", folded)
    return _ALIAS_INDEX.get(folded)


def parse_any_date(value: str) -> date | None:
    """Dates de codes-barres / textes : AAAA-MM-JJ, JJ/MM/AAAA, JJ.MM.AAAA, AAAAMMJJ, JJMMAAAA, MMJJAAAA (AAMVA)."""
    text = (value or "").strip()
    patterns = (
        (r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", (1, 2, 3)),
        (r"(\d{1,2})[-/. ](\d{1,2})[-/. ](\d{4})", (3, 2, 1)),
    )
    for pattern, (yi, mi, di) in patterns:
        match = re.search(pattern, text)
        if match:
            try:
                return date(int(match.group(yi)), int(match.group(mi)), int(match.group(di)))
            except ValueError:
                return None
    digits = re.sub(r"\D", "", text)
    if len(digits) == 8:
        try:
            if digits[:2] in ("19", "20"):
                return date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
            return date(int(digits[4:]), int(digits[2:4]), int(digits[:2]))
        except ValueError:
            return None
    return None


def _gender_of(value: str) -> str:
    folded = _fold(value)
    if folded in ("M", "H", "1", "MALE", "MASCULIN", "HOMME", "ذكر"):
        return "M"
    if folded in ("F", "2", "FEMALE", "FEMININ", "FEMME", "أنثى", "انثى"):
        return "F"
    return ""


def _id_type_of(value: str) -> str:
    if value.strip().lower() in ID_TYPES:
        return value.strip().lower()
    folded = _fold(value)
    for key, label in ID_TYPES.items():
        if folded == _fold(label):
            return key
    if folded.startswith("P") and ("PASS" in folded or len(folded) <= 2):
        return "passport"
    if any(word in folded for word in ("PERMIS", "DRIV", "LICEN", "DL")):
        return "permis"
    if folded in ("I", "ID", "CNI", "C") or any(word in folded for word in ("CARTE", "IDENT", "NATIONAL")):
        return "cni"
    return ""


def _apply_mapping(result: ExtractionResult, values: dict[str, str]) -> None:
    for key, value in values.items():
        value = str(value or "").strip()
        if not value:
            continue
        if key in ("birth_date", "id_expiry_date", "id_issue_date"):
            parsed = parse_any_date(value)
            result.fields[key] = parsed.isoformat() if parsed else ""
        elif key == "gender":
            result.fields[key] = _gender_of(value)
        elif key == "nationality":
            result.fields[key] = _nationality_from_text(value)
        elif key == "id_type":
            result.fields[key] = _id_type_of(value)
        elif key in ("last_name", "first_name"):
            result.fields[key] = " ".join(value.split())[:80]
        else:
            result.fields[key] = value[:120]


AAMVA_FIELDS = {"DCS": "last_name", "DAC": "first_name", "DCT": "first_name", "DAQ": "id_number",
                "DBB": "birth_date", "DBA": "id_expiry_date", "DBD": "id_issue_date", "DBC": "gender",
                "DAI": "id_issue_place", "DCG": "nationality"}


def parse_aamva(text: str) -> ExtractionResult | None:
    """Permis de conduire / carte d'identité nord-américains (PDF417 AAMVA, « ANSI … DCS… DAQ… »)."""
    if "ANSI " not in text and "AAMVA" not in text:
        return None
    values: dict[str, str] = {}
    for line in re.split(r"[\r\n\x1e\x1d]+", text):
        line = line.strip()
        # Le premier élément du sous-fichier suit l'en-tête « DL » / « ID » (ex. « DLDAQ123… »).
        for match in re.finditer(r"(?:^|DL|ID)(D[A-Z]{2})(.*)$", line):
            code, value = match.group(1), match.group(2).strip()
            if code in AAMVA_FIELDS and value and AAMVA_FIELDS[code] not in values:
                values[AAMVA_FIELDS[code]] = value
            break
    if "last_name" not in values and "id_number" not in values:
        return None
    for key in ("birth_date", "id_expiry_date", "id_issue_date"):
        raw = re.sub(r"\D", "", values.get(key, ""))
        if len(raw) == 8 and raw[:2] not in ("19", "20"):  # format américain MMJJAAAA
            values[key] = f"{raw[4:]}-{raw[:2]}-{raw[2:4]}"
    if values.get("first_name"):
        values["first_name"] = values["first_name"].replace(",", " ")
    result = ExtractionResult(format="aamva", engine="mrz", raw_text=text)
    _apply_mapping(result, values)
    result.fields.setdefault("id_type", "permis")
    result.fields["id_type"] = result.fields["id_type"] or "permis"
    result.confidence = 0.9
    result.message = MSG_BARCODE_OK
    return result


def parse_structured(text: str) -> ExtractionResult | None:
    """Texte structuré d'un QR / PDF417 : objet JSON, ou paires « CLÉ: valeur » / « CLÉ=valeur »."""
    stripped = (text or "").strip()
    values: dict[str, str] = {}
    fmt = ""
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except ValueError:
            data = None
        if isinstance(data, dict):
            fmt = "json"
            for label, value in data.items():
                key = _key_of(str(label))
                if key and isinstance(value, (str, int, float)) and key not in values:
                    values[key] = str(value)
    if not fmt:
        pairs = re.split(r"[\r\n;|\x1e\x1d\t]+", stripped)
        for pair in pairs:
            match = re.match(r"\s*([^:=]{1,40}?)\s*[:=]\s*(.+?)\s*$", pair)
            if not match:
                continue
            key = _key_of(match.group(1))
            if key and key not in values:
                values[key] = match.group(2)
        fmt = "cles"
    if len(values) < 2 or not (values.get("last_name") or values.get("id_number")):
        return None
    result = ExtractionResult(format=fmt, engine="mrz", raw_text=text)
    _apply_mapping(result, values)
    result.confidence = 0.8
    result.message = MSG_BARCODE_OK
    expiry = parse_any_date(result.fields.get("id_expiry_date", ""))
    if expiry and expiry < date.today():
        result.warn(MSG_EXPIRED)
    return result


def parse_scanner_input(raw: str, today: date | None = None) -> ExtractionResult:
    """Flux brut d'une douchette : MRZ (lignes séparées ou continues), sinon code-barres structuré, sinon échec."""
    text = (raw or "").replace("\x00", "")
    if not text.strip():
        return ExtractionResult(message=MSG_NO_INPUT, raw_text=text)
    result = parse_mrz(text, today)
    if result is not None:
        return result
    for parser in (parse_aamva, parse_structured):
        parsed = parser(text)
        if parsed is not None:
            if today is not None:
                expiry = parse_any_date(parsed.fields.get("id_expiry_date", ""))
                parsed.warnings = [w for w in parsed.warnings if w != MSG_EXPIRED]
                if expiry and expiry < today:
                    parsed.warn(MSG_EXPIRED)
            return parsed
    return ExtractionResult(message=MSG_UNRECOGNIZED, raw_text=text)


# ======================================================================== heuristiques sur le texte OCR
_DATE_RE = re.compile(r"(?<!\d)(\d{1,2}[./\- ]\d{1,2}[./\- ](?:19|20)\d{2}|(?:19|20)\d{2}[./-]\d{1,2}[./-]\d{1,2})(?!\d)")
_KEYWORDS = {
    "birth_date": ("NAISS", "NE LE", "NEE LE", "BIRTH", "DOB", "الميلاد", "الازدياد"),
    "id_expiry_date": ("EXPIR", "VALABLE", "VALIDIT", "FIN DE", "EXPIRY", "UNTIL", "الانتهاء", "صالحة"),
    "id_issue_date": ("DELIVR", "ISSUE", "EMIS", "EMISSION", "الإصدار", "الاصدار", "إصدار"),
}


def extract_from_text(text: str, today: date | None = None) -> ExtractionResult:
    """Lecture approximative d'un texte OCR sans MRZ : numéro, dates, noms, sexe, nationalité, type de pièce."""
    today = today or date.today()
    result = ExtractionResult(format="texte", engine="tesseract", raw_text=text or "")
    content = text or ""
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    folded_lines = [_fold(line) for line in lines]
    dated: list[tuple[date, str | None]] = []
    for index, line in enumerate(lines):
        for match in _DATE_RE.finditer(line):
            parsed = parse_any_date(match.group(1))
            if parsed is None:
                continue
            # Mot-clé sur la même ligne, avant la date ; à défaut, sur la ligne précédente (libellé au-dessus).
            kind = None
            for context in (_fold(line[: match.start()]), folded_lines[index - 1] if index else ""):
                kind = next((key for key, words in _KEYWORDS.items() if any(w in context for w in words)), None)
                if kind:
                    break
            dated.append((parsed, kind))
    for parsed, kind in dated:
        if kind and not result.fields.get(kind):
            result.fields[kind] = parsed.isoformat()
    unlabeled = sorted(d for d, kind in dated if kind is None)
    if unlabeled:
        past = [d for d in unlabeled if d <= today and (today - d).days > 365 * 10]
        future = [d for d in unlabeled if d > today]
        if past and not result.fields.get("birth_date"):
            result.fields["birth_date"] = past[0].isoformat()
        if future and not result.fields.get("id_expiry_date"):
            result.fields["id_expiry_date"] = future[-1].isoformat()
    number = re.search(r"(?:N[°º]|\bNO\b\.?|NUM[EÉ]RO|NUMBER|رقم)\s*[:.]?\s*(?=[A-Z0-9]*\d)([A-Z0-9]{6,20})\b",
                       content.upper())
    if number:
        result.fields["id_number"] = number.group(1)
    else:
        tokens = re.findall(r"\b(?=[A-Z0-9]*\d{6})[A-Z0-9]{8,18}\b", content.upper())
        if tokens:
            result.fields["id_number"] = sorted(tokens, key=lambda tok: (abs(len(tok) - 9), tok))[0]
    for key, pattern in (
        ("last_name", r"(?:\bNOM\b|SURNAME|FAMILY NAME)\s*[:/]?\s*([A-ZÀ-Ÿ' -]{2,40})"),
        ("first_name", r"(?:PR[EÉ]NOMS?|GIVEN NAMES?)\s*[:/]?\s*([A-ZÀ-Ÿ' -]{2,40})"),
    ):
        match = re.search(pattern, content.upper())
        if match:
            result.fields[key] = " ".join(match.group(1).split()).title()
    for key, pattern in (("last_name_ar", r"اللقب\s*[:：]?\s*([؀-ۿ ]{2,40})"),
                         ("first_name_ar", r"الاسم\s*[:：]?\s*([؀-ۿ ]{2,40})")):
        match = re.search(pattern, content)
        if match:
            result.fields[key] = " ".join(match.group(1).split())
    sex = re.search(r"(?:SEXE|SEX)\s*[:/]?\s*([MFH])\b", content.upper())
    if sex:
        result.fields["gender"] = _gender_of(sex.group(1))
    upper = _fold(content)
    for word, label in (("ALGERIEN", "Algérienne"), ("ALGERIA", "Algérienne"), ("جزائري", "Algérienne"),
                        ("FRANCAIS", "Française"), ("TUNISIEN", "Tunisienne"), ("MAROCAIN", "Marocaine")):
        if word in upper:
            result.fields["nationality"] = label
            break
    if any(word in upper for word in ("PASSEPORT", "PASSPORT", "جواز")):
        result.fields["id_type"] = "passport"
    elif any(word in upper for word in ("PERMIS", "DRIVING", "رخصة السياقة")):
        result.fields["id_type"] = "permis"
    elif any(word in upper for word in ("CARTE NATIONALE", "IDENTITY CARD", "IDENTITE", "بطاقة التعريف")):
        result.fields["id_type"] = "cni"
    expiry = parse_any_date(result.fields.get("id_expiry_date", ""))
    if expiry and expiry < today:
        result.warn(MSG_EXPIRED)
    result.confidence = 0.3 if result.has_data else 0.0
    result.message = MSG_TEXT_ONLY if result.has_data else MSG_NOTHING_FOUND
    return result


# ======================================================================== moteur OCR (Tesseract, facultatif)
_PROBE_TTL = 300.0
_probe: dict[str, Any] = {"at": 0.0, "ok": False, "version": "", "error": ""}
OCR_LANGS = ("fra", "ara", "eng")
MRZ_WHITELIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<"


def _windows_tesseract_candidates() -> list[Path]:
    """Emplacements de l'installeur Windows (UB Mannheim) : pour tous les utilisateurs ou pour l'utilisateur courant."""
    import os  # noqa: PLC0415

    roots = [os.environ.get(name, "") for name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
    roots += [r"C:\Program Files", r"C:\Program Files (x86)"]
    candidates = []
    for root in roots:
        if root:
            base = Path(root) / ("Programs" if root.lower().endswith("appdata\\local") else "")
            candidates += [Path(root) / "Tesseract-OCR" / "tesseract.exe", base / "Tesseract-OCR" / "tesseract.exe"]
    return candidates


def find_tesseract_cmd() -> str:
    """Chemin du binaire : TESSERACT_CMD (.env) > PATH > emplacements standard de l'installeur Windows."""
    if config.tesseract_cmd:
        return config.tesseract_cmd
    found = shutil.which("tesseract")
    if found:
        return found
    for candidate in _windows_tesseract_candidates():
        if candidate.is_file():
            return str(candidate)
    return ""


def _pytesseract():
    try:
        import pytesseract  # noqa: PLC0415 – dépendance facultative
    except ImportError:
        return None
    command = find_tesseract_cmd()
    if command:
        pytesseract.pytesseract.tesseract_cmd = command
    return pytesseract


def tesseract_status(force: bool = False) -> dict[str, Any]:
    """État du moteur : {ok, version, error} (sondé au plus toutes les 5 minutes)."""
    now = time.monotonic()
    if not force and _probe["at"] and now - _probe["at"] < _PROBE_TTL:
        return dict(_probe)
    module = _pytesseract()
    if module is None:
        _probe.update(at=now, ok=False, version="", error="Module Python pytesseract non installé.")
    else:
        try:
            version = str(module.get_tesseract_version())
            _probe.update(at=now, ok=True, version=version, error="")
        except Exception as exc:  # binaire absent, chemin TESSERACT_CMD invalide, droits…
            _probe.update(at=now, ok=False, version="", error=f"Binaire Tesseract introuvable : {exc}"[:300])
    return dict(_probe)


def tesseract_available() -> bool:
    """Vrai si pytesseract ET le binaire tesseract sont utilisables."""
    return bool(tesseract_status()["ok"])


def pdf_engine_available() -> bool:
    return importlib.util.find_spec("pypdfium2") is not None


def _otsu(gray: Image.Image) -> int:
    histogram = gray.histogram()[:256]
    total = sum(histogram)
    if not total:
        return 128
    sum_all = sum(i * h for i, h in enumerate(histogram))
    weight_bg = sum_bg = 0
    best, threshold = 0.0, 128
    for level, count in enumerate(histogram):
        weight_bg += count
        if not weight_bg:
            continue
        weight_fg = total - weight_bg
        if not weight_fg:
            break
        sum_bg += level * count
        mean_bg = sum_bg / weight_bg
        mean_fg = (sum_all - sum_bg) / weight_fg
        between = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
        if between > best:
            best, threshold = between, level
    return threshold


def preprocess(image: Image.Image) -> Image.Image:
    """Rotation EXIF, niveaux de gris, contraste automatique, redimensionnement (1600 à 2600 px de large)."""
    image = ImageOps.exif_transpose(image)
    gray = image.convert("L")
    gray = ImageOps.autocontrast(gray, cutoff=1)
    width, height = gray.size
    if width < 1600:
        factor = 1600 / max(1, width)
        gray = gray.resize((1600, max(1, int(height * factor))), Image.Resampling.LANCZOS)
    elif width > 2600:
        factor = 2600 / width
        gray = gray.resize((2600, max(1, int(height * factor))), Image.Resampling.LANCZOS)
    return gray.filter(ImageFilter.SHARPEN)


def mrz_zone(gray: Image.Image, share: float = 0.42) -> Image.Image:
    """Zone basse binarisée (seuil d'Otsu) où se trouve la MRZ des passeports et des cartes."""
    width, height = gray.size
    zone = gray.crop((0, int(height * (1 - share)), width, height))
    threshold = _otsu(zone)
    return zone.point(lambda p: 255 if p > threshold else 0)


def _languages(module) -> str:
    try:
        installed = set(module.get_languages(config=""))
    except Exception:
        installed = set(OCR_LANGS)
    chosen = [lang for lang in OCR_LANGS if lang in installed]
    return "+".join(chosen) or "eng"


def ocr_image(image: Image.Image, today: date | None = None) -> ExtractionResult:
    """OCR d'une image : MRZ d'abord (zone basse, puis image retournée, puis page), ensuite texte de la page."""
    module = _pytesseract()
    if module is None or not tesseract_available():
        return ExtractionResult(engine="none", message=MSG_NO_TESSERACT)
    prepared = preprocess(image)
    mrz_config = f"--psm 6 -c tessedit_char_whitelist={MRZ_WHITELIST}"
    texts: list[str] = []
    found = None
    try:
        for candidate in (mrz_zone(prepared), mrz_zone(prepared.rotate(180)), prepared):
            text = module.image_to_string(candidate, lang="eng", config=mrz_config) or ""
            texts.append(text)
            found = find_mrz(text)
            if found is not None:
                break
        page_text = ""
        try:
            page_text = module.image_to_string(prepared, lang=_languages(module), config="--psm 3") or ""
        except Exception:  # langue manquante : nouvel essai en anglais seul
            page_text = module.image_to_string(prepared, lang="eng", config="--psm 3") or ""
    except Exception as exc:
        logger.warning("OCR Tesseract en échec : %s", exc)
        return ExtractionResult(engine="tesseract", message=MSG_OCR_ERROR, raw_text="\n".join(texts))
    if found is None:
        found = find_mrz(page_text)
    text_result = extract_from_text(page_text, today)
    if found is None:
        text_result.raw_text = page_text
        return text_result
    result = parse_mrz_lines(found[0], found[1], today)
    result.engine = "tesseract"
    result.raw_text = page_text
    # Le texte de la page complète ce que la MRZ ne contient pas (date de délivrance, noms arabes…).
    for key in ("id_issue_date", "last_name_ar", "first_name_ar"):
        if text_result.fields.get(key) and not result.fields.get(key):
            result.fields[key] = text_result.fields[key]
    result.confidence = round(result.confidence * 0.95, 2)
    return result


def render_pdf_first_page(data: bytes) -> Image.Image | None:
    """Première page d'un PDF rendue en image (pypdfium2) ; None si le module est absent."""
    try:
        import pypdfium2 as pdfium  # noqa: PLC0415 – dépendance facultative
    except ImportError:
        return None
    document = pdfium.PdfDocument(data)
    try:
        page = document[0]
        width = page.get_width() or 595
        scale = min(4.0, max(1.5, 2000 / width))
        image = page.render(scale=scale).to_pil()
        page.close()
        return image.convert("RGB")
    finally:
        document.close()


# ======================================================================== validation et stockage des fichiers
SCAN_FOLDER = "id_scans"
PENDING_FOLDER = "_attente"
PENDING_MAX_AGE = 2 * 24 * 3600
MAX_PIXELS = 40_000_000
MAX_SIDE = 10_000
STORED_MAX_SIDE = 3500
PDF_MAX_PAGES = 20
REL_PATH_RE = re.compile(r"^id_scans/(\d+|_attente)/[0-9a-f]{32}\.(jpg|png|pdf)$")
FILE_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(jpg|png|pdf)$")
MIME_BY_EXT = {"jpg": "image/jpeg", "png": "image/png", "pdf": "application/pdf"}
# Fonctions actives d'un PDF : refusées (un scan n'en contient jamais).
_PDF_ACTIVE = re.compile(rb"/(?:JavaScript|JS|Launch|EmbeddedFile|RichMedia|XFA|SubmitForm|ImportData|GoToR)\b")
_PDF_ESCAPED_NAME = re.compile(rb"/[A-Za-z]*#[0-9A-Fa-f]{2}")


def media_root() -> Path:
    return config.data_dir / "media"


def scans_root() -> Path:
    return media_root() / SCAN_FOLDER


def max_upload_bytes() -> int:
    return int(config.id_scan_max_bytes)


@dataclass
class PreparedFile:
    """Fichier validé, prêt à être écrit : contenu nettoyé, extension, type MIME, image pour l'OCR."""

    content: bytes
    ext: str
    mime_type: str
    image: Image.Image | None = None
    is_pdf: bool = False
    pdf_readable: bool = False


def sniff(data: bytes) -> str:
    """Type réel d'après les octets magiques : jpeg, png, pdf ou chaîne vide."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"%PDF-"):
        return "pdf"
    return ""


def _open_image(data: bytes) -> Image.Image:
    invalid = "Image invalide ou corrompue."
    too_big = "Image trop grande : 40 mégapixels et 10 000 pixels de côté au maximum."
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as probe:
                if probe.format not in ("JPEG", "PNG"):
                    raise BusinessError("Format non pris en charge : PNG, JPEG ou PDF uniquement.")
                width, height = probe.size
                if width * height > MAX_PIXELS or max(width, height) > MAX_SIDE:
                    raise BusinessError(too_big)
                probe.verify()
            image = Image.open(BytesIO(data))
            image.load()
    except BusinessError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise BusinessError(too_big) from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError) as exc:
        raise BusinessError(invalid) from exc
    return image


def _reencode(image: Image.Image, kind: str) -> tuple[bytes, Image.Image]:
    """Réencode l'image sur un nouveau support : aucune métadonnée (EXIF, GPS, ICC, commentaires) n'est recopiée."""
    image = ImageOps.exif_transpose(image)
    if max(image.size) > STORED_MAX_SIDE:
        image.thumbnail((STORED_MAX_SIDE, STORED_MAX_SIDE), Image.Resampling.LANCZOS)
    if kind == "png":
        mode = "RGBA" if image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info else "RGB"
        if image.mode == "L":
            mode = "L"
    else:
        mode = "L" if image.mode == "L" else "RGB"
    converted = image.convert(mode)
    clean = Image.new(mode, converted.size)
    clean.paste(converted)
    buffer = BytesIO()
    if kind == "png":
        clean.save(buffer, format="PNG", optimize=True)
    else:
        clean.save(buffer, format="JPEG", quality=90, optimize=True)
    return buffer.getvalue(), clean


def _validate_pdf(data: bytes) -> bool:
    """Contrôle structurel d'un PDF ; renvoie True si la première page a pu être rendue (lecture possible)."""
    if b"%%EOF" not in data[-2048:]:
        raise BusinessError("PDF invalide ou tronqué.")
    if _PDF_ACTIVE.search(data) or _PDF_ESCAPED_NAME.search(data):
        raise BusinessError("PDF refusé : il contient du contenu actif (script, pièce jointe, formulaire).")
    if not pdf_engine_available():
        return False
    import pypdfium2 as pdfium  # noqa: PLC0415 – dépendance facultative
    try:
        document = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        raise BusinessError("PDF illisible ou protégé par un mot de passe.") from exc
    try:
        pages = len(document)
    finally:
        document.close()
    if pages < 1:
        raise BusinessError("PDF vide.")
    if pages > PDF_MAX_PAGES:
        raise BusinessError("PDF trop long : 20 pages au maximum pour une pièce d'identité.")
    return True


def prepare_file(data: bytes) -> PreparedFile:
    """Valide un fichier téléversé (taille, octets magiques, structure) et le nettoie (réencodage des images)."""
    if not data:
        raise BusinessError("Fichier vide.")
    if len(data) > max_upload_bytes():
        raise BusinessError("Fichier trop volumineux : taille maximale autorisée dépassée.")
    kind = sniff(data)
    if not kind:
        raise BusinessError("Format non pris en charge : PNG, JPEG ou PDF uniquement.")
    if kind == "pdf":
        readable = _validate_pdf(data)
        return PreparedFile(content=data, ext="pdf", mime_type="application/pdf", is_pdf=True, pdf_readable=readable)
    image = _open_image(data)
    if image.format == "PNG" and kind != "png" or image.format == "JPEG" and kind != "jpeg":
        raise BusinessError("Contenu du fichier incohérent avec son type.")
    content, clean = _reencode(image, kind)
    ext = "png" if kind == "png" else "jpg"
    return PreparedFile(content=content, ext=ext, mime_type=MIME_BY_EXT[ext], image=clean)


def _folder_for(guest_id: int | None) -> str:
    return str(int(guest_id)) if guest_id else PENDING_FOLDER


def write_scan(prepared: PreparedFile, guest_id: int | None) -> str:
    """Écrit le fichier sous un nom aléatoire ; renvoie le chemin relatif à data/media."""
    folder = scans_root() / _folder_for(guest_id)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{secrets.token_hex(16)}.{prepared.ext}"
    target = folder / name
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(prepared.content)
    tmp.replace(target)
    return f"{SCAN_FOLDER}/{_folder_for(guest_id)}/{name}"


def resolve_scan(relative: str) -> Path | None:
    """Chemin absolu d'un scan à partir de son chemin relatif ; None si le chemin est invalide ou hors du dossier."""
    if not relative or not REL_PATH_RE.match(relative):
        return None
    path = (media_root() / relative).resolve()
    root = scans_root().resolve()
    if root not in path.parents or not path.is_file():
        return None
    return path


def mime_of(relative: str) -> str:
    return MIME_BY_EXT.get(relative.rsplit(".", 1)[-1], "application/octet-stream")


def delete_scan_files(relatives: list[str]) -> None:
    """Supprime des fichiers de scan (après validation de la transaction) ; les erreurs sont journalisées."""
    for relative in relatives:
        path = resolve_scan(relative)
        if path is None:
            continue
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("Suppression du scan %s impossible : %s", relative, exc)


def _copy_to_guest(relative: str, guest_id: int) -> str:
    """Copie un scan « en attente » dans le dossier du client ; renvoie le nouveau chemin relatif."""
    source = resolve_scan(relative)
    if source is None:
        return ""
    folder_name = _folder_for(guest_id)
    if relative.startswith(f"{SCAN_FOLDER}/{folder_name}/"):
        return relative
    folder = scans_root() / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / source.name
    shutil.copyfile(source, target)
    return f"{SCAN_FOLDER}/{folder_name}/{source.name}"


def purge_pending(max_age: float = PENDING_MAX_AGE) -> int:
    """Supprime les scans « en attente » jamais rattachés à un client depuis plus de ``max_age`` secondes."""
    folder = scans_root() / PENDING_FOLDER
    if not folder.is_dir():
        return 0
    limit = time.time() - max_age
    removed = 0
    for path in folder.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < limit:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def restore_scan_files(archive: Any, prefix: str) -> list[str]:
    """Restaure depuis une archive ZIP de sauvegarde (``zipfile.ZipFile``) les scans rangés sous
    ``<prefix>id_scans/``. Seuls les chemins produits par ce module sont acceptés (pas de « zip slip »)."""
    folder_prefix = prefix + SCAN_FOLDER + "/"
    restored = []
    for info in archive.infolist():
        if info.is_dir() or not info.filename.startswith(folder_prefix):
            continue
        relative = SCAN_FOLDER + "/" + info.filename[len(folder_prefix):]
        if not REL_PATH_RE.match(relative) or info.file_size > max_upload_bytes() * 2:
            continue
        target = media_root() / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(info))
        restored.append(relative)
    return sorted(restored)


# ======================================================================== journal et application à la fiche client
def _context_ids(reservation: Reservation | None, guest: Guest | None) -> tuple[int | None, int | None]:
    guest_id = guest.id if guest else (reservation.guest_id if reservation else None)
    return guest_id, reservation.id if reservation else None


async def record_scan(db: AsyncSession, result: ExtractionResult, *, source: str, user: User, hs: HotelSettings,
                      side: str = "recto", guest: Guest | None = None, reservation: Reservation | None = None,
                      file_path: str = "", mime_type: str = "", file_size: int = 0) -> IdScan:
    """Journalise une lecture (table id_scans) et l'action dans le journal d'activité."""
    if source not in ID_SCAN_SOURCES:
        raise BusinessError("Source de numérisation invalide.")
    if side not in ID_SCAN_SIDES:
        raise BusinessError("Face de la pièce invalide (recto ou verso).")
    guest_id, reservation_id = _context_ids(reservation, guest)
    scan = IdScan(
        guest_id=guest_id, reservation_id=reservation_id, source=source, side=side, file_path=file_path,
        mime_type=mime_type, engine=result.engine, raw_text=(result.raw_text or "")[:20000], raw_mrz=result.mrz,
        fields_json=json.dumps(result.to_dict(), ensure_ascii=False), checksums_valid=result.checksums_valid,
        confidence=result.confidence, status=result.status, message=result.message,
        user_id=user.id, username=user.username, created_at=now_of(hs), file_size=file_size,
    )
    db.add(scan)
    await db.flush()
    await log_action(db, user, "id_scan_read", "id_scan", scan.id,
                     f"{ID_SCAN_SOURCES[source]} – {result.status} – {result.message}", now_of(hs))
    return scan


def _clean_values(values: dict[str, Any]) -> dict[str, Any]:
    """Champs vérifiés par la réception (formulaire de la fenêtre de numérisation) → valeurs typées."""
    get = lambda key: str(values.get(key) or "").strip()  # noqa: E731
    id_type = get("id_type") or "cni"
    if id_type not in ID_TYPES:
        raise BusinessError("Type de pièce d'identité invalide.")
    gender = get("gender")
    if gender not in GENDERS:
        raise BusinessError("Sexe invalide.")
    cleaned = {
        "last_name": get("last_name")[:80], "first_name": get("first_name")[:80],
        "last_name_ar": get("last_name_ar")[:80], "first_name_ar": get("first_name_ar")[:80],
        "birth_place": get("birth_place")[:120], "nationality": get("nationality")[:80],
        "id_number": get("id_number")[:60], "id_issue_place": get("id_issue_place")[:120],
        "id_type": id_type, "gender": gender,
        "birth_date": to_date(get("birth_date"), "Date de naissance", required=False),
        "id_issue_date": to_date(get("id_issue_date"), "Date de délivrance", required=False),
        "id_expiry_date": to_date(get("id_expiry_date"), "Date d'expiration", required=False),
    }
    if not cleaned["last_name"] or not cleaned["first_name"]:
        raise BusinessError("Nom et prénom requis.")
    if not normalize_id_number(cleaned["id_number"]):
        raise BusinessError("Numéro de pièce d'identité requis.")
    return cleaned


async def _find_by_number(db: AsyncSession, id_number: str) -> Guest | None:
    """Même numéro sous un autre type de pièce (ex. CNI saisie « passeport ») : retenu s'il est unique."""
    norm = normalize_id_number(id_number)
    matches = list((await db.scalars(select(Guest).where(Guest.id_number_norm == norm))).unique().all())
    return matches[0] if len(matches) == 1 else None


@dataclass
class ApplyOutcome:
    guest: Guest
    created: bool
    files_to_delete: list[str]


async def apply_to_guest(db: AsyncSession, values: dict[str, Any], hs: HotelSettings, user: User, *,
                         scans: list[IdScan] | None = None, guest: Guest | None = None,
                         reservation: Reservation | None = None) -> ApplyOutcome:
    """Crée ou complète la fiche client à partir des champs vérifiés ; dédoublonne par pièce ; rattache les scans.

    Cible : le client imposé (check-in : client de la réservation ; fiche CRM), sinon le client qui possède déjà
    cette pièce (même type et numéro, ou même numéro s'il est unique), sinon une nouvelle fiche. Les champs
    d'identité lus sur la pièce remplacent les anciens ; téléphone, e-mail, adresse, notes… sont conservés.
    Les fichiers remplacés sont renvoyés pour suppression APRÈS la validation de la transaction.
    """
    cleaned = _clean_values(values)
    target = guest or (reservation.guest if reservation is not None else None)
    if target is None:
        target = await crm.find_duplicate(db, cleaned["id_type"], cleaned["id_number"])
        if target is None:
            target = await _find_by_number(db, cleaned["id_number"])
    created = target is None
    data: dict[str, Any] = {key: (getattr(target, key) if target is not None else "") for key in crm.GUEST_TEXT_FIELDS}
    if target is not None:
        data.update(birth_date=target.birth_date, id_issue_date=target.id_issue_date, company_id=target.company_id,
                    gender=target.gender, id_type=target.id_type)
    else:
        data.update(birth_date=None, id_issue_date=None, company_id=None, gender="", id_type="cni")
    for key, value in cleaned.items():
        if key == "id_expiry_date":
            continue
        if value not in (None, ""):
            data[key] = value
    # Le type de pièce et le sexe vérifiés s'appliquent même vides (« < » dans la MRZ = non précisé).
    data["id_type"] = cleaned["id_type"]
    if cleaned["gender"] or created:
        data["gender"] = cleaned["gender"]
    saved = await crm.save_guest(db, target, data, hs, user)
    if cleaned["id_expiry_date"] is not None:
        saved.id_expiry_date = cleaned["id_expiry_date"]
    now = now_of(hs)
    to_delete: list[str] = []
    for scan in scans or []:
        if scan.file_path:
            new_path = _copy_to_guest(scan.file_path, saved.id)
            if new_path and new_path != scan.file_path:
                to_delete.append(scan.file_path)
            if new_path:
                attr = "id_scan_back_path" if scan.side == "verso" else "id_scan_path"
                old = getattr(saved, attr)
                if old and old != new_path:
                    to_delete.append(old)
                    await db.execute(update(IdScan).where(IdScan.file_path == old).values(file_path=""))
                setattr(saved, attr, new_path)
                scan.file_path = new_path
        scan.guest_id = saved.id
        if reservation is not None:
            scan.reservation_id = reservation.id
        scan.status = "applied"
        scan.applied_at = now
    saved.id_scanned_at = now
    await db.flush()
    details = f"{saved.full_name} – {ID_TYPES[saved.id_type]} {saved.id_number}"
    if reservation is not None:
        details += f" – réservation {reservation.code}"
    await log_action(db, user, "id_scan_applied", "guest", saved.id, details, now)
    return ApplyOutcome(guest=saved, created=created, files_to_delete=to_delete)


def guest_summary(guest: Guest, today: date | None = None) -> dict[str, Any]:
    """Résumé JSON d'une fiche (sélection dans le formulaire de réservation)."""
    return {
        "id": guest.id, "name": guest.full_name, "phone": guest.phone, "id_number": guest.id_number,
        "id_type": guest.id_type, "status": guest.status,
        "id_expiry_date": guest.id_expiry_date.isoformat() if guest.id_expiry_date else "",
        "expired": guest.id_expired_on(today) if today else False,
    }

