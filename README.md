

https://github.com/user-attachments/assets/cb305799-2ecc-438a-ac13-07ad6beec306

# Nylo AI — Local Chat Website

A Flask website at `http://127.0.0.1:5000` that uses a Selenium-controlled Chrome window to interact with the ChatGPT website. It starts the Flask website **independently** of Chrome, so a slow/broken Chrome launch does not stop the local page from loading.

## On macOS

1. Install Google Chrome if you don't have it.
2. Open Terminal in this project folder.
3. Install dependencies:

   ```bash
   python3 -m pip install -r requirements.txt
   ```

4. Start the server:

   ```bash
   python3 app.py
   ```

5. Open **http://127.0.0.1:5000** in your regular browser.
6. A Chrome window will open separately. Sign in to ChatGPT in that window (first run only).
7. Type a message in Nylo AI. Click **Open / reconnect Chrome** if the connection status reports an error.

## File structure

- `app.py`: Flask routes, Selenium session, chat, file and image handling.
- `templates/index.html`: responsive user interface (no build step).
- `requirements.txt`: Python dependencies.
- `selenium_profile/`: automatically created local Chrome profile (retains sign-in; keep it private).

## Troubleshooting

- **Website refuses to connect:** Keep the terminal running and look for `Running on http://127.0.0.1:5000`.
- **Port 5000 in use:** change `PORT = 5000` in `app.py` to `5050`, and use `http://127.0.0.1:5050`.
- **Chrome failed:** Install or update Google Chrome. Make sure no other copy of this script is using `selenium_profile`.
- **A message can't send / response isn't detected:** ChatGPT's page markup may have changed; the CSS selectors in `app.py` may need updating. The page UI is not an official stable API.
- **`ModuleNotFoundError`:** run `python3 -m pip install -r requirements.txt` in the same Python environment used to start the app.
- **`TemplateNotFound`:** ensure the HTML file is under the `templates/` subfolder.

This interface is local-only. File attachments are temporarily saved for Selenium uploads, then removed. The Chrome profile remains on disk and contains your browser sign-in state. Do not commit or share `selenium_profile`.
