# APK Maker Studio

This is a standalone, Arabic-first recreation of the visible Base44 app. It includes the dashboard, six-step project wizard, build history, engine setup page, Arabic/English toggle, dark/light themes, and responsive layout. Project metadata is stored in the browser. The local API starts a real GitHub Actions build and only exposes a download after the APK/AAB and its checksum/signature report pass verification.

## Run locally

1. Install Python 3.10 or newer.
2. Double-click `start.bat`, or open a terminal in this folder and run `py -3 server.py` (or `python server.py`).
3. Open [http://127.0.0.1:8787](http://127.0.0.1:8787).
4. Open **Build engine → Set up automatically** and enter a GitHub personal access token. The setup creates or updates a private repository named `apk-maker-builds` and uploads the workflow from this folder.
5. Upload a ZIP (up to 65 MB) or enter a public GitHub repository URL, complete the six steps, and start the build.

The server binds to loopback only. Do not expose it to the public internet: this version has no user authentication and keeps the GitHub token in process memory. Reconnect the engine after restarting the server. The token is sent to the local server and then to GitHub's API; it is never saved in browser storage or written to the app's data files.

## GitHub access

For a classic personal access token, grant `repo` and `workflow` scopes. Automatic private-repository creation needs the account-level permission to create repositories. For a fine-grained token, allow private repository creation and give the build repository **Contents: write**, **Workflows: write**, and **Actions: write** permissions. GitHub requires Actions write permission for workflow dispatch and workflow permissions for changing a `.github/workflows` file ([GitHub workflow API](https://docs.github.com/en/rest/actions/workflows), [GitHub repository contents API](https://docs.github.com/en/rest/repos/contents)).

GitHub does not create a personal access token from an email address and password. The account owner must create/authorize the token once. This app deliberately never asks for or stores a GitHub password.

## Build behavior and limits

- Web projects are wrapped in a native Android WebView app. Android Gradle and Flutter projects build with their own toolchains.
- The APK is the default output; AAB is optional. The workflow signs the output and verifies the signature and SHA-256 digest before making it downloadable.
- No build is simulated. If GitHub is disconnected, the project is saved as a draft and the app shows the engine setup page.
- ZIP uploads are limited to 65 MB because this version transfers them through the GitHub Contents API. Uploaded source ZIPs stay in the private build repository so saved projects can be rebuilt. GitHub Actions stores build artifacts for seven days; this server downloads a verified result into `data/artifacts/`.
- Unless you add `KEYSTORE_BASE64`, `KEYSTORE_PASSWORD`, and `KEY_ALIAS` as Actions repository secrets, each build creates a fresh signing key. That is fine for testing and first installs; it does not preserve the signing identity for updates to an already-installed app.
- GitHub-hosted runner availability and usage limits depend on the account's current plan. Review the plan before repeated builds.
- Third-party projects can contain untrusted build scripts. Only build source code you trust. The workflow runs that code inside a GitHub-hosted runner, not on the local server.

The workflow is in [build-apk.yml](build-apk.yml). No service token, APK, or success state is bundled with this project.

