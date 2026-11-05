"""Autorisation Google Drive avec un compte utilisateur (OAuth2) : à exécuter UNE fois sur un PC avec navigateur.

    python scripts/google_drive_auth.py client_secret.json
    python scripts/google_drive_auth.py client_secret.json --output data/google/token.json --port 8765

Étapes :
1. Dans Google Cloud Console : créer un projet, activer l'API Google Drive, créer un identifiant OAuth 2.0 de
   type « application de bureau » et télécharger son fichier client_secret.json.
2. Lancer ce script : le navigateur s'ouvre, connectez-vous avec le compte Google de l'hôtel et acceptez l'accès
   (portée drive.file : l'application ne voit que les fichiers qu'elle a elle-même créés).
3. Le fichier token.json produit (refresh_token, client_id, client_secret) est à copier dans data/google/ du
   serveur ou à importer dans Administration > Sauvegardes > Association Google Drive.

Méthode alternative sans navigateur : un compte de service (credentials.json) avec lequel on partage le dossier Drive.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "data" / "google" / "token.json"


def authorize(client_secret: Path, output: Path, port: int = 0, open_browser: bool = True) -> dict:
    """Lance le consentement OAuth2 dans le navigateur local et écrit le jeton utilisateur dans ``output``."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not client_secret.is_file():
        raise FileNotFoundError(f"Fichier client_secret introuvable : {client_secret}")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
    credentials = flow.run_local_server(port=port, open_browser=open_browser,
                                        authorization_prompt_message="Ouvrez ce lien dans votre navigateur : {url}",
                                        success_message="Autorisation accordée : vous pouvez fermer cette fenêtre.")
    if not credentials.refresh_token:
        raise RuntimeError("Google n'a pas fourni de refresh_token : révoquez l'accès de l'application dans le "
                           "compte Google (myaccount.google.com/permissions) puis relancez le script.")
    data = json.loads(credentials.to_json())
    data["type"] = "authorized_user"
    data["email"] = account_email(credentials)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        output.chmod(0o600)
    except OSError:  # Windows : pas de droits POSIX
        pass
    return data


def account_email(credentials) -> str:
    """Adresse du compte autorisé (affichée dans l'administration) ; vide si l'API ne la fournit pas."""
    try:
        from googleapiclient.discovery import build

        service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        about = service.about().get(fields="user(emailAddress)").execute()
        return str((about.get("user") or {}).get("emailAddress", ""))
    except Exception:  # information facultative : ne doit jamais faire échouer l'autorisation
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Autorisation Google Drive (jeton utilisateur OAuth2) pour les sauvegardes")
    parser.add_argument("client_secret", type=Path, help="fichier client_secret.json téléchargé depuis Google Cloud Console")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help=f"fichier token.json à produire (défaut : {DEFAULT_OUTPUT})")
    parser.add_argument("--port", type=int, default=0, help="port local du serveur de redirection OAuth (0 = automatique)")
    parser.add_argument("--no-browser", action="store_true", help="n'ouvre pas le navigateur, affiche seulement le lien")
    args = parser.parse_args(argv)
    try:
        data = authorize(args.client_secret, args.output, args.port, not args.no_browser)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1
    account = data.get("email") or "compte Google"
    print(f"Jeton enregistré pour {account} dans {args.output}.")
    print("Copiez ce fichier dans data/google/ du serveur, ou importez-le dans Administration > Sauvegardes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
