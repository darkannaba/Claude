# PMS / CRM hôtelier – Annaba (v2.0)

Logiciel de gestion hôtelière complet pour un hôtel de 30 chambres à Annaba (Algérie), conçu pour
fonctionner **entièrement en réseau local, sans Internet** : planning, réservations, check-in / check-out,
audit de nuit, facturation en DZD, CRM, fiche de police bilingue français / arabe, points de vente,
housekeeping, rapports TO / ADR / RevPAR, exports comptables et sauvegardes automatiques.

**Pile technique** : Python 3.10+ · FastAPI · SQLAlchemy 2 (asynchrone) · SQLite en mode WAL · Alembic ·
Jinja2 · Tailwind CSS compilé localement · Alpine.js · Chart.js · ReportLab (police DejaVu Sans, mise en forme
arabe et bidirectionnelle) · Argon2.

---

## 1. Installation

### Windows (poste de réception)

1. Installer **Python 3.10 ou plus récent** depuis python.org en cochant « Add python.exe to PATH ».
2. Double-cliquer sur `scripts\install.bat` : environnement virtuel, dépendances, base de données et comptes.
3. **Noter les trois mots de passe** affichés à la fin de l'installation (ils ne sont affichés qu'une fois).
4. Lancer `scripts\run.bat` : le serveur démarre et le navigateur s'ouvre sur http://localhost:8000.

### Linux / macOS

```bash
scripts/install.sh
```

```bash
scripts/run.sh
```

### Installation manuelle

```bash
python -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python scripts/seed_prod.py
```

```bash
.venv/bin/python main.py
```

Les autres postes du réseau local accèdent à l'application par `http://<adresse-IP-du-serveur>:8000`.

### Comptes créés à l'initialisation

| Identifiant | Rôle | Interface |
|---|---|---|
| `admin` | Administrateur | Tout, y compris paramètres, utilisateurs, sauvegardes, exports comptables |
| `reception` | Réception | Planning, séjours, clients, POS, audit de nuit, factures, rapports |
| `gouvernante` | Gouvernante | Écran housekeeping uniquement (adapté au téléphone) |

Un changement de mot de passe est imposé à la première connexion. Pour régénérer un mot de passe perdu :

```bash
python scripts/seed_prod.py --reset-password admin
```

Pour former le personnel sur des données fictives (5 clients, 5 réservations, articles POS) :

```bash
python scripts/seed_prod.py --demo
```

---

## 2. Fonctionnalités

### Front desk et planning
- **Tableau de bord** : occupation actuelle, arrivées et départs, encaissements du jour par mode, état des
  30 chambres, TO / ADR / RevPAR du mois, CA des 14 derniers jours, alertes (audit en retard, stock bas).
- **Planning Gantt** sur 7 à 31 jours, filtres par type et étage ; un clic sur une case libre ouvre une
  réservation pré-remplie.
- **Réservations** : disponibilité en temps réel, estimation du séjour (saison, week-end), capacité par type,
  acompte (arrhes) à la création, modification, annulation motivée, no-show.
- **Séjour** : check-in (pièce d'identité obligatoire, paramétrable), changement de chambre en cours de
  séjour, extras, encaissements, check-out avec contrôle du solde et émission de la facture définitive.

### Audit de nuit et clôture journalière
- L'audit **fige définitivement** le prix de chaque nuitée et de la taxe de séjour dans `folio_items`
  (clé unique par nuit : aucune double imputation possible).
- Un changement de tarif dans l'administration ne modifie **jamais** une nuit déjà clôturée ni une facture émise.
- Exécution **automatique** chaque nuit (heure paramétrable), rattrapage des journées manquées (31 jours),
  no-show automatique des arrivées non présentées, rapport PDF de clôture journalière bilingue.
- Correction d'une écriture : suppression si la journée est ouverte, sinon **contre-passation** (écriture
  négative tracée) ; seules les écritures non facturées sont corrigibles, et les nuitées par l'admin seulement.

### Facturation (DZD)
- Tarifs saisis TTC, TVA extraite par taux (hébergement, restauration, services), taxe de séjour par
  personne et par nuit (hors TVA).
- **Facture définitive** au départ : numérotation continue `FAC-AAAA-00001`, totaux figés, écritures
  verrouillées. Avant le départ : facture **proforma** (nuits prévisionnelles signalées).
- Facture **A4 bilingue** (ventilation de la TVA, montant en toutes lettres, NIF / NIS / RC / AI, QR code)
  et **ticket 80 mm**. Entreprises clientes : remise négociée, NIF et RC sur la facture.

### CRM et registre légal
- Fiche complète : noms en latin **et en arabe**, naissance (date, lieu), nationalité, profession, adresse,
  pièce d'identité (type, numéro, délivrance), entreprise, préférences, notes.
- **Détection automatique des doublons** par n° de CNI / passeport (numéro normalisé, index unique) :
  à la réservation, une pièce déjà connue rattache automatiquement la fiche existante.
- Doublons potentiels (même nom + naissance, même téléphone) et **fusion** de fiches (admin).
- Statuts **VIP** et **liste noire** (motif obligatoire, réservé à l'admin) : la liste noire bloque
  réservations et arrivées sauf forçage administrateur. Alerte automatique des **impayés**.
- **Fiche de police bilingue français / arabe** en un clic, pré-remplie avec tous les accompagnateurs
  (famille) ; une fiche individuelle supplémentaire par accompagnateur muni d'une pièce d'identité.

### Points de vente (modules désactivés par défaut)
- Restaurant / Café / Bar, Parking, Salle de réunion, activables un par un.
- Additions : règlement **comptant** (espèces, CIB / EDAHABIA, carte, virement, chèque) ou **Room Charge**
  (imputation sur la note de la chambre, une écriture par taux de TVA).
- Catalogue administrable, **stock** pour le bar / café (vente, réapprovisionnement, inventaire, alertes
  de stock bas, historique des mouvements).

### Housekeeping
- Tableau par statut (À nettoyer, Libres, Occupées, Maintenance), priorisation des arrivées du jour,
  filtrage par étage, interface tactile pour la gouvernante.
- Option « statuts réservés à la réception » : la gouvernante consulte sans modifier.

### Rapports et comptabilité
- TO, ADR, RevPAR sur une période et tendance mensuelle sur l'année ; CA par jour, semaine ou mois ;
  encaissements par mode de paiement (graphiques Chart.js).
- **Export comptable** Excel (feuille d'écritures et synthèse) et CSV (séparateur « ; », compatible Excel) :
  journal des ventes (HT, TVA, TTC par taux) et journal de caisse.

### Sauvegardes
- Sauvegarde **automatique quotidienne** dans `backups/AAAA-MM-JJ_HHMMSS/` (copie cohérente via l'API
  SQLite, compatible WAL), conservation des N dernières.
- Interface d'administration : sauvegarder, télécharger, restaurer (sauvegarde de sécurité préalable),
  importer un fichier `.db` (contrôle d'intégrité et de structure).
- En ligne de commande, utilisable depuis le Planificateur de tâches Windows :
  `python scripts/backup.py`, `python scripts/backup.py --list` et `python scripts/backup.py --restore NOM`.

### Périphériques
- **Douchette / scanner USB** : la recherche universelle (F2) accepte un code de réservation, un QR code
  (imprimé sur la fiche de police, la facture et le ticket), un n° de facture, un n° d'addition ou un n° de pièce.
  Hors d'un champ de saisie, une frappe rapide terminée par Entrée est reconnue automatiquement.
- **Impression** : mode « navigateur » (boîte d'impression ouverte automatiquement) ou mode « réseau »
  (envoi direct sur le port RAW 9100) : tickets ESC/POS pour les imprimantes thermiques 80 mm, PDF pour les
  imprimantes A4 réseau qui acceptent l'impression PDF directe. Page de test dans Administration > Paramètres.

---

## 3. Sécurité

- Mots de passe hachés **Argon2** (rehachage automatique), changement imposé à la première connexion.
- Sessions signées (cookie `HttpOnly`, `SameSite=Lax`, `Secure` activable) ; nouvelle session à chaque connexion.
- Jeton **CSRF** obligatoire sur toutes les requêtes modifiantes (formulaires et requêtes JavaScript).
- Requêtes paramétrées par l'ORM, motifs de recherche échappés (aucune injection SQL).
- Verrouillage temporaire après 5 échecs de connexion (par identifiant et par adresse IP).
- En-têtes HTTP : Content-Security-Policy, X-Frame-Options, X-Content-Type-Options, Referrer-Policy.
- **RBAC** à trois rôles appliqué côté serveur sur chaque route, avec des menus adaptés au rôle.
- **Journal d'activité** consultable par l'admin : connexions, séjours, encaissements, corrections, paramètres.

---

## 4. Configuration

- **Technique** (`.env`, voir `.env.example`) : port, base, clé de session, dossier et rétention des
  sauvegardes, verrouillage de connexion, planificateur. Modèle Pydantic validé au démarrage.
- **Métier** (Administration > Paramètres, aucune valeur figée dans le code) : identité de l'hôtel en
  français et en arabe, NIF / NIS / RC / AI, devise, taux de TVA, taxe de séjour, jours et majoration du
  week-end, heures d'audit et de sauvegarde, préfixes de numérotation, textes de la fiche de police,
  imprimantes, fuseau horaire.

> Les taux livrés (TVA 19 %, taxe de séjour 100 DA par personne et par nuit, tarifs des chambres) sont des
> **valeurs de départ** : faites-les valider par votre expert-comptable (taux applicable à l'hébergement,
> barème de la taxe de séjour selon le classement de l'établissement) avant la mise en service.

---

## 5. Structure du projet

```
main.py                     lancement (un seul processus : SQLite + planificateur intégré)
alembic.ini, migrations/    migrations Alembic, appliquées automatiquement au démarrage
app/
  main.py                   application FastAPI, middlewares, gestion des erreurs, planificateur
  config.py                 configuration Pydantic (.env)
  database.py               moteur asynchrone SQLite WAL, sessions, migrations
  deps.py                   authentification, RBAC, CSRF, exécution des actions métier
  templating.py             Jinja2 : filtres, icônes SVG locales, navigation par rôle
  models/                   user, room, customer, booking, invoice, pos, audit, system
  routers/                  auth, pms, crm, housekeeping, pos, reports, admin
  services/                 pricing, folio, booking, night_audit, invoicing, pdf, printing, crm, pos,
                            reports, exports, backup, scheduler, security, settings, seed
  templates/                gabarit principal, écrans par module, macros d'interface
  static/                   css/app.css (Tailwind compilé), js/, vendor/ (Alpine, Chart.js), fonts/ (DejaVu)
scripts/                    seed_prod.py, backup.py, install.bat / run.bat, install.sh / run.sh
tests/                      97 tests (pages, parcours complets, services) – couverture 95 %
```

---

## 6. Développement

```bash
pip install -r requirements-dev.txt
```

```bash
python -m pytest --cov=app
```

Le CSS est déjà compilé dans `app/static/css/app.css`. Node.js n'est nécessaire que pour modifier le design :

```bash
npm install
```

```bash
npm run build:css
```

Une classe Tailwind composée dynamiquement dans un template (par exemple `room-{{ status }}`) doit être
ajoutée à la `safelist` de `tailwind.config.js` ; un test vérifie la présence de ces classes dans le CSS.

Après une modification des modèles, générer une migration :

```bash
alembic revision --autogenerate -m "description"
```

---

## 7. Notes d'exploitation

- **Un seul processus** : ne pas lancer plusieurs workers (SQLite et planificateur intégré).
- **HTTPS** : l'application vise le réseau local. Pour un accès distant, la placer derrière un reverse proxy
  TLS et définir `COOKIE_SECURE=true`.
- **Tickets thermiques** : les imprimantes ESC/POS ne disposent pas de police arabe ; les tickets réseau sont
  donc imprimés en caractères latins. Les PDF (fiche de police, factures, ticket PDF) sont bilingues.
- **Arabe dans les PDF** : la police DejaVu Sans, embarquée, est lisible mais non calligraphique.
- **CSP** : Alpine.js nécessite `'unsafe-eval'` dans `script-src` ; tous les scripts restent locaux.
- **Documentation de l'API** : le schéma OpenAPI est servi à l'administrateur sur `/admin/openapi.json`.
  Swagger UI n'est pas servi, car il dépend d'un CDN.
- **Version 1** : le schéma de données a changé. Une base créée avec la version 1 n'est pas migrée :
  installez la version 2 sur une base neuve.
