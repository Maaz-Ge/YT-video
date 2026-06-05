# Docker deployment guide (baby steps)

This replaces running `python app.py` inside **tmux** on the server. Docker gives you:

- **ffmpeg** and Python deps baked into the image (no manual `apt install` / `pip install` on the server)
- **Auto-restart** if the process crashes (`restart: unless-stopped`)
- **Same data** — your existing `projects/` folder is mounted into the container

---

## What you need on the server

| Item | Notes |
|------|--------|
| Linux server | Ubuntu 22.04/24.04 is fine |
| Docker Engine | Install once (see Step 1) |
| Docker Compose plugin | Usually included with modern Docker |
| Your `.env` file | Must contain `OPENAI_API_KEY=...` |
| Your `projects/` folder | All generated videos/images stay here on disk |

You **do not** need a Python venv on the server anymore for this app (only Docker).

---

## Step 1 — Install Docker on the server (one-time)

SSH into your server, then:

```bash
# Ubuntu — official convenience script
curl -fsSL https://get.docker.com | sudo sh

# Allow your user to run docker without sudo (log out/in after this)
sudo usermod -aG docker $USER
```

Verify:

```bash
docker --version
docker compose version
```

If `docker compose` is missing, install the plugin:

```bash
sudo apt-get update
sudo apt-get install -y docker-compose-plugin
```

---

## Step 2 — Put the app on the server

Pick a folder, e.g. `/opt/tatterveil` or keep your current path.

### Option A — You already run from a git folder

```bash
cd /path/to/YT-AISYSTEM/YT-video
git pull   # get Dockerfile, docker-compose.yml, etc.
```

### Option B — Copy from your PC

On your PC (PowerShell), from the repo:

```bash
scp -r YT-video user@YOUR_SERVER_IP:/opt/tatterveil/
```

On the server:

```bash
cd /opt/tatterveil
```

---

## Step 3 — Keep your secrets and data

On the server, inside `YT-video` (same folder as `docker-compose.yml`):

```bash
ls -la
# You should see: projects/   .env   docker-compose.yml   Dockerfile
```

1. **`.env`** — If you already have one from the tmux setup, leave it there.  
   If not:

   ```bash
   cp .env.example .env
   nano .env   # paste OPENAI_API_KEY=sk-...
   ```

2. **`projects/`** — Must stay on the host. Docker mounts `./projects` → `/app/projects` inside the container.  
   **Do not delete this folder** when switching to Docker.

---

## Step 4 — Stop the old tmux process

```bash
tmux ls
# find your session, e.g. scene-studio

tmux attach -t scene-studio
# press Ctrl+C to stop python app.py
# or from another shell:
tmux kill-session -t scene-studio
```

Make sure nothing is still listening on port **5001**:

```bash
sudo ss -tlnp | grep 5001
```

If something is still bound, stop that process before starting Docker.

---

## Step 5 — Build and start with Docker Compose

From the `YT-video` directory:

```bash
cd /opt/tatterveil   # or your path

docker compose build
docker compose up -d
```

Check status:

```bash
docker compose ps
docker compose logs -f --tail=100
```

Open in browser: `http://YOUR_SERVER_IP:5001`

Press `Ctrl+C` to leave logs (container keeps running).

---

## Step 6 — Firewall (if you cannot reach the site)

If the app works on the server but not from your PC:

```bash
# Ubuntu ufw example
sudo ufw allow 5001/tcp
sudo ufw status
```

---

## Day-to-day commands (cheat sheet)

| Task | Command |
|------|---------|
| Start | `docker compose up -d` |
| Stop | `docker compose down` |
| Restart after code update | `git pull && docker compose build && docker compose up -d` |
| View logs | `docker compose logs -f` |
| Shell inside container | `docker compose exec scene-studio bash` |
| Check ffmpeg in container | `docker compose exec scene-studio ffmpeg -version` |

---

## Updating the app after you change code

```bash
cd /opt/tatterveil
git pull                    # or upload new files
docker compose build --no-cache   # only if dependencies/Dockerfile changed
docker compose up -d
```

Your `projects/` data is **not** inside the image — it stays on disk via the volume mount.

---

## Migrating from your old Python venv setup

| Old (tmux) | New (Docker) |
|------------|----------------|
| `python -m venv venv` | Not needed |
| `pip install -r requirements.txt` | Done at `docker compose build` |
| `apt install ffmpeg` | Included in Dockerfile |
| `python app.py` | `gunicorn` inside container (via compose) |
| `./projects` on server | Same path, mounted as volume |
| `.env` on server | Same file, `env_file:` in compose |

You can remove the old `venv/` folder on the server **after** Docker works, to save space. Keep `projects/` and `.env`.

---

## Optional — Nginx reverse proxy (HTTPS on port 443)

If you already use Nginx for other sites, add a server block:

```nginx
server {
    listen 80;
    server_name studio.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:5001;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 600s;
    }
}
```

Then use Certbot for HTTPS. Point `proxy_pass` at `127.0.0.1:5001` because Docker publishes that port on localhost.

**Faster image loading (recommended for 4K projects):** serve previews and full PNGs directly from disk instead of through Gunicorn:

```nginx
    # Host path must match docker-compose volume: ./projects:/app/projects
    location ~ ^/projects/([^/]+)/previews/(.+)\.(png|jpe?g)$ {
        alias /var/www/project/YT-video/projects/$1/images/thumbs/$2.jpg;
        add_header Cache-Control "public, max-age=31536000, immutable";
    }
    location ~ ^/projects/([^/]+)/images/(.+)$ {
        alias /var/www/project/YT-video/projects/$1/images/$2;
        add_header Cache-Control "public, max-age=31536000, immutable";
    }
```

Place these **above** the `location / { proxy_pass ... }` block. The app still generates thumbnails; nginx just delivers them faster.

---

## Troubleshooting

### Container exits immediately

```bash
docker compose logs
```

Common causes: missing `.env`, invalid `OPENAI_API_KEY`, or port 5001 already in use.

### Export ZIP fails (“ffmpeg not installed”)

Inside container:

```bash
docker compose exec scene-studio which ffmpeg
docker compose exec scene-studio ffmpeg -version
```

If missing, rebuild: `docker compose build --no-cache && docker compose up -d`

### Projects disappeared

They are only on the host if the volume mount is correct. Check:

```bash
ls -la ./projects
docker compose exec scene-studio ls -la /app/projects
```

Both should list the same project IDs.

### Generation stuck / duplicate status after restart

In-memory jobs (active export/regeneration) are lost when the container restarts. Refresh the page; persisted data is in `projects/<id>/status.json` and `scenes.json` on disk.

### “Address already in use” on 5001

Stop tmux/old process or change the host port in `docker-compose.yml`:

```yaml
ports:
  - "5002:5001"   # host:container — browse http://server:5002
```

---

## Why one Gunicorn worker?

This app keeps **in-memory** queues for exports and regenerations. Running **multiple** Gunicorn workers would split that state across processes. The Dockerfile uses:

- `--workers 1`
- `--threads 8`

That matches how you ran `python app.py` with `threaded=True`, but is more stable for production.

---

## Quick local test (on your PC before server)

```bash
cd YT-video
cp .env.example .env   # add a real key
docker compose build
docker compose up
```

Visit http://localhost:5001 — `Ctrl+C` stops it; use `docker compose up -d` for background.

---

## Server feels slow?

See **[SERVER-PERFORMANCE.md](./SERVER-PERFORMANCE.md)** for a step-by-step SSH checklist (CPU, RAM, disk, ffmpeg export, logs).
