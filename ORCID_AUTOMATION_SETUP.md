# Automatic ORCID publication updates

This folder contains a GitHub Actions workflow that checks your public ORCID
record each day and adds new works to this website automatically.

## One-time setup

1. Upload the updated website folder to the root of your GitHub repository.
   The folders named `.github`, `scripts`, and `data` must be uploaded too.
2. Create a free **Public API** client at
   <https://orcid.org/developer-tools>. Give it the redirect URI
   `https://pssponduru.github.io/`.
3. In your GitHub repository go to **Settings → Secrets and variables → Actions**.
4. Under **Repository secrets**, add these two secrets:
   - `ORCID_CLIENT_ID` — the Client ID from ORCID
   - `ORCID_CLIENT_SECRET` — the Client Secret from ORCID
5. Open the **Actions** tab, select **Sync new ORCID works**, then click
   **Run workflow** once. It will test the connection and add only works that
   are not already on your website.

After that, GitHub checks daily. GitHub can run a scheduled workflow later than
the exact clock time, which is normal.

## What it changes

- Creates one readable HTML page in `publications/` for each new public ORCID
  record.
- Adds its link under **Newly synchronized works** on the home page.
- Adds the page to `sitemap.xml` so Google can discover it.
- Commits the change directly to the `main` branch, which GitHub Pages
  publishes automatically.

## PDF safety setting

Automatic PDF downloading is deliberately **off**. A DOI normally opens a
publisher page, not a legal direct PDF download.

If you own or have permission to mirror PDFs from specific public repositories,
add these GitHub **Actions variables**:

- `ALLOW_ORCID_PDF_DOWNLOAD` = `true`
- `TRUSTED_PDF_HOSTS` = a comma-separated list, for example
  `zenodo.org,arxiv.org,osf.io,figshare.com`

The workflow will then download only direct `.pdf` links from exactly those
domains, only when the file is a valid PDF and smaller than 5 MB. Otherwise it
still creates the publication page with the official source/DOI link.
