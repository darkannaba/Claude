"""Alias du générateur PDF du projet (app/services/pdf.py).

Le cahier des charges du module de charte graphique désigne le générateur sous le nom ``pdf_generator`` : ce module
n'en est pas une copie, c'est le même objet module (``app.services.pdf_generator is app.services.pdf``), de sorte que
``from app.services.pdf_generator import invoice_pdf`` et ``from app.services.pdf import invoice_pdf`` désignent
exactement les mêmes fonctions.
"""
import sys

from app.services import pdf as _pdf

sys.modules[__name__] = _pdf
