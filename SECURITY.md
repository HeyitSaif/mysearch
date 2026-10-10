# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem. Report it privately with GitHub's private vulnerability
reporting: on the repository's **Security** tab, choose **Report a vulnerability**. Include the commit or version, your
macOS version, the steps to reproduce and what you expected to happen. Reports are read; a reply is not guaranteed
within a fixed time.

## What leaves your Mac

* Your files, the search indexes, the file-name index and the models stay on the machine.
* Setup downloads Python packages (from PyPI, and the mlx-vlm repository on GitHub) and the model weights (from Hugging
  Face). These are downloads from public hosts; they carry no information about your files.
* **Ask with cloud models** is off by default. When you switch it on, the question, the top passages and the recent chat
  turns go to the provider you choose. Personal identifiers are masked first (`cloud_redact`, on by default). Provider keys
  are kept in the macOS Keychain.

## Access to the local service

The HTTP service listens on `127.0.0.1` only, and it refuses any request whose `Host` header is not `127.0.0.1`,
`localhost` or `[::1]` on its own port. That stops a web page from reaching it through DNS rebinding. A request that
carries an `Origin` header must name the service's own address.

**The token.** A random value (`secrets.token_urlsafe(24)`) is written once to `config.json` and is never printed. Every
`/api/` route and the MCP endpoint `/mcp` need it, sent either in the `X-MSE-Token` header or as the query parameter `t`.
A request without a matching token gets `401`. The comparison is constant-time, and a service with no token configured
refuses every request.

**The web pages.** `/`, `/index.html`, `/settings…` and Quick Search (`/?mini=1`) need the token as well. Those pages
embed it, so their scripts can call the API. Asked for without a valid token, a page answers `401` with a short note that
says how to open MySearch. The page is sent with `Referrer-Policy: no-referrer`, so the token in its URL is not passed on
to any other site. `/static/` (the stylesheet, the scripts and the icon) is public and holds no token.

**What `?t=` is for.** A browser cannot set a header on the requests it makes for an `<img>`, a `<video>` or `<audio>`
element, or a PDF shown in a frame, so those carry the token as `t`. The app's own page loads carry it as `t` too, because
WebKit page loads and reloads do not resend a header the app set. `mse ui` opens the web UI with `t` in its URL, so that
token is kept in the browser's history for that page.

**Who the token keeps out.** Other accounts on this Mac, and web pages open in your browser: a page on another site
cannot read the responses, and it cannot set the header. The token is not a boundary between programs that run as the
same user, because those can read `config.json`.

**Files from your folders.** The service sends PDFs, images, audio and video with their own types. Every other file,
including HTML, XHTML, XML and SVG, is sent as plain text, with `Content-Security-Policy: sandbox` and
`X-Content-Type-Options: nosniff`. A file in an indexed folder therefore cannot run script in the page's own origin, where
the token is. The sandbox policy is left off PDFs, because the sandbox rule would stop the PDF viewer drawing in the preview
frame. That reasoning has not been checked in the app's WebKit view.

## Scope

Reports about the local service, the file access rules (`mse/ignore.py`, `mse/macindex.py`), the masking
(`mse/redact.py`) and the Mac app are in scope. Problems in third-party packages or models should go to their own
projects.
