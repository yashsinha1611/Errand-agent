# Deploying Errand on AWS

Use a **personal AWS account** for this project.

## Which AWS service

The app keeps its state in SQLite files on local disk, streams each turn over a long-lived HTTP response, and
uses a supervisor so the crash demo really restarts the process. That fits one small Linux server.

| Option | Fit | Why |
| --- | --- | --- |
| **Lightsail instance** (recommended) | Best | One VM, fixed monthly price, static IP, snapshots. Simplest console. |
| **EC2 instance** | Good | Same setup script; step-by-step below. |
| ECS Express Mode (containers) | Later | AWS's recommended container path. Needs persistent storage or Postgres first, because container disks are wiped on restart. A `Dockerfile` is included. |
| App Runner | No | Closed to new customers since 30 April 2026. |
| Lambda | No | Long streaming responses and local SQLite don't suit it. |

## Deploy on Lightsail (about 15 minutes)

**1. Create the server.** In the Lightsail console: *Create instance* → region **Mumbai (ap-south-1)** → Linux →
**OS only: Ubuntu 24.04 LTS** → the smallest plan is enough → create. Then *Networking*: attach a **static IP**,
and in the IPv4 firewall allow **HTTPS (443)** (SSH 22 and HTTP 80 are open by default; HTTP is needed for the
HTTPS certificate). Restrict SSH to your own IP if you can.

**2. Get a hostname.** HTTPS needs a domain name:

- **Your own domain:** add an `A` record, for example `errand.yourdomain.com`, pointing to the static IP.
- **No domain:** use the free `sslip.io` name for the IP, for example `13-233-45-67.sslip.io` for `13.233.45.67`.
  It resolves to your IP and works with automatic certificates (it is a shared service, so treat it as a demo
  convenience).

**3. Copy the code to the server** (Lightsail lets you download the SSH key under *Account → SSH keys*):

```bash
scp -i LightsailKey.pem errand-agent.zip ubuntu@13.233.45.67:~
ssh -i LightsailKey.pem ubuntu@13.233.45.67
sudo apt-get update && sudo apt-get install -y unzip
unzip errand-agent.zip && cd errand-agent
```

**4. Run the setup script** with your hostname:

```bash
sudo DOMAIN=13-233-45-67.sslip.io ./deploy/setup.sh
```

It installs Caddy (automatic HTTPS), copies the app to `/opt/errand-agent`, runs it as a locked-down systemd
service on localhost, puts a password in front of the whole site, and prints the URL, user (`demo`) and a
random password. **Save the password; it is shown once.**

**5. Add your Claude key:**

```bash
sudo nano /etc/errand/errand.env          # set ANTHROPIC_API_KEY=sk-ant-...
sudo systemctl restart errand
```

**6. Check it:**

```bash
curl -u demo:YOUR_PASSWORD https://13-233-45-67.sslip.io/api/health
# {"ok": true, "pid": ..., "model": "claude-haiku-5-5"}
```

Open the URL in a browser, log in, and the header should show **Claude: claude-haiku-5-5**.

## Deploy on EC2 (step by step)

**1. Launch the instance.** EC2 console → region **Asia Pacific (Mumbai) ap-south-1** → *Launch instance*:

| Setting | Choose |
| --- | --- |
| Name | `errand` |
| AMI | **Ubuntu Server 24.04 LTS**, 64-bit **(x86)** |
| Instance type | `t3.small` (2 GB). `t3.micro` (1 GB) also runs it if you want the cheapest option. |
| Key pair | *Create new key pair* → RSA → `.pem` → download it (you get it only once) |
| Network settings | *Create security group* with three rules: **SSH** from **My IP**; **HTTPS** from anywhere; **HTTP** from anywhere (needed to issue the certificate) |
| Storage | 8–16 GB gp3 is plenty |

Click *Launch instance*.

**2. Give it a fixed address.** *Elastic IPs* → *Allocate Elastic IP address* → *Allocate* → select it →
*Actions → Associate* → choose the `errand` instance. Note the IP, say `13.233.45.67`.

**3. Get a hostname.** Point your domain's `A` record at the Elastic IP, or use `13-233-45-67.sslip.io`.

**4. Copy the code and connect.** On macOS/Linux, first run `chmod 400 errand.pem`. On Windows PowerShell,
`scp` and `ssh` are built in; if ssh complains the key is too open, right-click the file → Properties → Security
and leave only your own user.

```bash
scp -i errand.pem errand-agent.zip ubuntu@13.233.45.67:~
ssh -i errand.pem ubuntu@13.233.45.67
sudo apt-get update && sudo apt-get install -y unzip
unzip errand-agent.zip && cd errand-agent
```

**5. Install and start everything:**

```bash
sudo DOMAIN=13-233-45-67.sslip.io ./deploy/setup.sh
```

Save the printed user and password.

**6. Add your key and restart:**

```bash
sudo nano /etc/errand/errand.env          # ANTHROPIC_API_KEY=sk-ant-...
sudo systemctl restart errand
curl -u demo:YOUR_PASSWORD https://13-233-45-67.sslip.io/api/health
```

**7. Guard the bill.** *Billing → Budgets → Create budget* → a monthly cost budget of a few dollars with an
email alert. Public IPv4 addresses, including an attached Elastic IP, are billed hourly, so when you're done:
terminate the instance, then *release* the Elastic IP.

**If something fails:** `sudo journalctl -u errand -n 50` (app), `sudo journalctl -u caddy -n 50` (HTTPS). A
certificate error almost always means port 80 is closed or the hostname doesn't point at the Elastic IP yet.

## Running it

| Task | Command |
| --- | --- |
| Live logs | `sudo journalctl -u errand -f` |
| Restart | `sudo systemctl restart errand` |
| Status | `systemctl status errand caddy` |
| Deploy a new version | upload the new zip, unzip, `cd errand-agent`, rerun `sudo DOMAIN=... ./deploy/setup.sh` (keeps your data and key; prints a new password) |
| Reset demo data | **Faults → Reset world** in the console, or `sudo systemctl stop errand && sudo rm -rf /opt/errand-agent/var/* && sudo systemctl start errand` |
| Backup | Lightsail *Snapshots* (or EBS snapshots on EC2); the data is in `/opt/errand-agent/var` |

The crash demo works on the server: the injected crash ends the child process, the supervisor restarts it, and
systemd restarts everything if the supervisor itself ever stops.

## Security checklist

- **Password in front of everything.** The app has no user login of its own, and its demo endpoints (faults,
  clock, reset) are powerful. Caddy's password protects both. Don't remove it on a public server.
- **Turn demo controls off after recording.** Set `ERRAND_DEMO=0` in `/etc/errand/errand.env` and restart.
- **The key stays on the server**, in `/etc/errand/errand.env` (readable by root only). Never commit it.
- **Set a monthly spend limit** in the Claude Console, so a leaked password can't run up a bill. Each
  conversation is also capped at $0.50 by the app.
- **Privacy.** Set `TRACE_CAPTURE_CONTENT=false` if real people will type real details: traces then store
  hashes instead of message text.
- Keep the OS patched: `sudo apt-get update && sudo apt-get upgrade -y`.

## Cost and clean-up

You pay for the instance (the smallest Lightsail plan is a few dollars a month; check current pricing) plus
Claude usage (well under a cent per conversation on Haiku 5.5). When you're done, **delete the instance and
release the static IP**: an unattached static IP or Elastic IP can still be charged.

## Later: containers and scale

To run on ECS Express Mode, build the included `Dockerfile` (`docker build -t errand .`), push it to ECR, and
give the service persistent storage at `/app/var`, or better, move state to Postgres (RDS). The design note's
"What breaks first at 100×" section lists the order: async outbox workers, Postgres, a background reconciler,
per-provider retry budgets, and OpenTelemetry export.
