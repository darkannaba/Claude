# Gestion hôtelière – Annaba (PMS / CRM)

Application web sur mesure pour un hôtel de 30 chambres à Annaba (Algérie) :
planning, réservations, check-in / check-out, clients (CRM), fiche de police,
factures en DZD (A4 et ticket 80 mm), tarifs saisonniers, housekeeping, administration.

**Stack** : Python 3.10+ · FastAPI · SQLAlchemy · SQLite · Jinja2 + Tailwind (mode clair / sombre) · ReportLab.

---

## 1. Installation et lancement

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows : .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

Puis ouvrez **http://localhost:8000** dans le navigateur (depuis un autre poste du réseau local : `http://<adresse-du-serveur>:8000`).

Au **premier lancement** :
- la base `data/hotel.db` est créée à partir du modèle `data/hotel_template.db` (structure, paramètres, 30 chambres) ;
- trois comptes sont créés avec des **mots de passe aléatoires affichés une seule fois dans la console** :

| Identifiant | Rôle | Accès |
|---|---|---|
| `admin` | Administrateur | Tout, y compris Administration |
| `reception` | Réception | Planning, réservations, clients, encaissements, housekeeping |
| `gouvernante` | Housekeeping | Tableau de bord des chambres uniquement |

Changez ces mots de passe dans **Administration > Utilisateurs**. Si vous les avez perdus :

```bash
python -m app.services.seed --reset-password admin
```

Documentation de l'API (Swagger) : http://localhost:8000/docs

---

## 2. Ce que fait l'application

### Front desk & planning
- **Tableau de bord** : arrivées et départs du jour, taux d'occupation, état des 30 chambres, encaissements du jour.
- **Planning** (diagramme de Gantt) sur 7, 14 ou 30 jours, filtrable par type de chambre et par étage. Clic sur une case libre = nouvelle réservation pré-remplie.
- **Réservations** : création (chambre libre contrôlée, capacité par type), modification des dates ou de la chambre tant que le client n'est pas arrivé, annulation, no-show.
- **Arrivée / départ** : check-in exige une pièce d'identité ; le check-out ramène le séjour à la date réelle (départ anticipé ou tardif) et refuse un solde impayé sauf case « départ malgré solde impayé ».
- **Changement de chambre** à tout moment pendant le séjour : l'occupation est découpée par segments, l'ancienne chambre passe « À nettoyer » et la nouvelle « Occupée ».

### Clients (CRM)
- Fiche complète : nom, prénom, téléphone, email, adresse, nationalité, date de naissance, type et numéro de pièce (passeport, CNI, autre), entreprise, préférences, notes.
- Recherche instantanée par nom, téléphone, numéro de pièce ou email. **Compatible lecteur USB / code-barres** : le champ a le focus à l'ouverture, et un numéro de pièce exact ouvre directement la fiche.
- Historique des séjours, nuitées et dépenses cumulées.
- Compagnons de voyage, rattachés à la réservation et à la fiche de police.

### Spécificités locales
- **Fiche de police** : PDF A4 généré à chaque demande depuis la réservation (voyageur principal + compagnons, signature). Titre, mention légale et pied de fiche sont modifiables dans Administration > Paramètres.
- **Devise DZD** et **taxe de séjour** par personne et par nuitée (100 DA par défaut, activable/désactivable).
- **TVA** : les tarifs sont saisis **TTC**, la TVA est extraite (HT = TTC / (1 + taux)) et affichée sur la facture.

### Tarification & facturation
- **Tarifs saisonniers** par type de chambre, avec dates de début et de fin (fin exclue) et priorité en cas de chevauchement.
- **Majoration du week-end** (jours configurables, 4 et 5 = vendredi et samedi par défaut).
- **Remise entreprise** : rattachement d'une entreprise cliente à la réservation, avec remise automatique sur les nuitées.
- **Acomptes (arrhes)**, paiements et remboursements, modes espèces / carte / virement / chèque.
- **Factures** A4 ou **ticket 80 mm**, numéro attribué à la première impression (les réimpressions gardent le même numéro), séquence configurable par préfixe.

### Housekeeping
- Tableau par statut : Libre, À nettoyer, Occupée (verrouillée), En maintenance.
- Option **« Réserver les statuts de nettoyage à la Réception / Admin »** : si activée, la gouvernante voit l'état des chambres mais ne peut plus le modifier.

### Modules annexes (feature flags)
- **Restaurant / Bar**, **Salle de réunion**, **Parking** : chacun **désactivé par défaut**, activable un par un dans Administration > Modules.
- Un module désactivé n'apparaît ni dans les choix de prestations ni dans les extras.
- Catalogue de prestations par module (Administration > Catalogue des prestations) ; la réception les porte au folio du client.

### Configuration sans valeur en dur
Tous les paramètres métier sont en base et éditables dans **Administration > Paramètres** : nom de l'hôtel, adresse, NIF / RC, devise, taux de TVA, taxe de séjour, week-end, heures d'arrivée et de départ, fuseau horaire, préfixe des factures, règles de housekeeping, modèle de fiche de police. Les tarifs des chambres se modifient dans **Administration > Chambres & types** et **Tarifs saisonniers**.

Les valeurs fournies au départ sont **indicatives** : « Hôtel Annaba (à personnaliser) », 8 000 / 11 000 / 16 000 DA par nuit, TVA 19 %. **Faites valider le taux de TVA applicable et le régime de la taxe de séjour par votre expert-comptable avant la mise en production.**

---

## 3. Structure du projet

```
main.py                    point d'entrée (python main.py)
requirements.txt
.env.example
app/
  main.py                  création de l'application FastAPI, gestion des erreurs, routeurs
  config.py                chemins, base de données, clé de session (configuration technique)
  database.py              moteur SQLAlchemy, session, init
  deps.py                  authentification, rôles (RBAC), rendu des templates, flash messages
  models/                  modèles SQLAlchemy
    admin.py               utilisateurs, paramètres, modules, entreprises, prestations
    property.py            types de chambres, chambres, tarifs saisonniers
    guests.py              clients (CRM)
    reservations.py        réservations, segments d'occupation, compagnons, paiements, extras, factures
  services/                logique métier (sans HTTP)
    availability.py        disponibilité, création, dates, changement de chambre, arrivée, départ, annulation
    pricing.py             tarification d'une nuit (saison, week-end, remise)
    folio.py               folio : nuitées, extras, HT/TVA, taxe de séjour, paiements, solde
    pdf.py                 fiche de police, facture A4 et ticket 80 mm (ReportLab)
    settings.py            paramètres métier par défaut, modules, fuseau horaire
    seed.py                initialisation, comptes, réinitialisation de mot de passe
    auth.py                hachage PBKDF2 des mots de passe
    common.py              erreurs métier et validations de saisie
  routers/                 une route = une fonction ; aucune règle métier dans les routeurs
    auth.py  dashboard.py (tableau de bord + planning)  reservations.py  guests.py
    housekeeping.py  admin.py
  templates/               pages Jinja2 (base.html, dossiers par domaine)
  static/                  app.css (jetons clair/sombre), app.js (thème, recherche, confirmations)
data/
  hotel_template.db        base modèle livrée (structure, 30 chambres, sans comptes)
tests/                     17 tests : règles métier, PDF, parcours HTTP, rôles
```

---

## 4. Tests

```bash
python -m pytest -q
```

Les tests couvrent : tarification (week-end, remise), calcul du folio (HT/TVA, taxe de séjour, solde), conflits de réservation, capacité, pièce d'identité obligatoire, départ avec solde, changement de chambre, annulation / no-show, génération des PDF, parcours HTTP complet (réservation → arrivée → extra → paiement → départ → factures), recherche scanner, restrictions par rôle et édition des paramètres.

---

## 5. Points d'attention et limites connues

- **Mise en page** : Tailwind est chargé depuis le CDN `cdn.tailwindcss.com` ; le poste qui consulte l'application doit avoir accès à Internet (ou remplacez-le par une version locale).
- **Sécurité** : l'application est prévue pour un réseau local de confiance. Avant toute exposition sur Internet, placez-la derrière un reverse proxy en HTTPS et ajoutez une protection CSRF.
- **Facturation** : les nuitées sont calculées à partir des tarifs en vigueur au moment de l'affichage ou de l'impression. Le numéro de facture est fixé à la première impression, mais son contenu suit les tarifs courants tant que le séjour n'est pas clôturé. Un gel des tarifs par nuit (audit de nuit) est une évolution naturelle.
- **Caractères** : les PDF utilisent la police Helvetica (ReportLab), adaptée aux noms en alphabet latin. Les noms en caractères arabes ne s'afficheront pas correctement sur les PDF sans police dédiée.
- **Modules annexes** : le restaurant, la salle de réunion et le parking sont gérés comme catalogue de prestations portées au folio. Il n'y a pas (encore) de gestion de tables, de stock ni de réservation de salle avec créneaux.
- **Une base SQLite** convient à un poste ou à un petit réseau local ; un seul serveur doit écrire dans la base à la fois.

---

## 6. Sauvegarde

La totalité des données est dans `data/hotel.db`. Sauvegardez ce fichier (l'arrêt du serveur avant copie est recommandé). `data/.secret_key` est la clé de session : la conserver évite de déconnecter les utilisateurs au redémarrage.
