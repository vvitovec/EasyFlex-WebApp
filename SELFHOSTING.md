# EasyFlex doma na čistém Ubuntu

Tenhle návod je připravený pro úplně čistý Ubuntu server a pro člověka, který Docker nikdy nenastavoval.

Výsledek:
- poběží web EasyFlexu,
- poběží worker pro zpracování fronty,
- poběží vlastní PostgreSQL databáze,
- a před tím poběží Caddy jako jednoduchá webová brána.

## 1. Co si připravit

Budete potřebovat:
- starý počítač s Ubuntu, který běží 24/7,
- přístup do terminálu,
- GitHub přístup k repu,
- OpenAI API key,
- údaje do ABRA,
- a heslo, kterým se budete hlásit jako `admin`.

## 2. Přihlášení na server

Na Ubuntu se přihlaste a otevřete Terminál.

Pak zadejte:

```bash
cd ~
```

## 3. Instalace Dockeru a Gitu

Zkopírujte do terminálu celý tento blok:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg git openssl
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable docker
sudo systemctl start docker
sudo usermod -aG docker $USER
```

Po dokončení:
- odhlaste se z Ubuntu,
- znovu se přihlaste,
- znovu otevřete Terminál.

Pak ověřte, že Docker funguje:

```bash
docker --version
docker compose version
git --version
```

## 4. Stažení EasyFlexu

Pokud ještě nemáte repo na serveru:

```bash
cd ~
git clone https://github.com/vvitovec/EasyFlex-WebApp.git
cd EasyFlex-WebApp
```

Pokud už repo na serveru máte:

```bash
cd ~/EasyFlex-WebApp
git pull --ff-only
```

## 5. Vytvoření `.env`

Vygenerujte si dvě dlouhá tajemství:

```bash
cd ~/EasyFlex-WebApp
export POSTGRES_PASSWORD="$(openssl rand -hex 24)"
export EASYFLEX_SECRET_KEY="$(openssl rand -hex 48)"
```

Teď vytvořte `.env`:

```bash
cat > .env <<EOF
POSTGRES_DB=easyflex
POSTGRES_USER=easyflex
POSTGRES_PASSWORD=$POSTGRES_PASSWORD

EASYFLEX_ENV=production
EASYFLEX_SECRET_KEY=$EASYFLEX_SECRET_KEY
EASYFLEX_ADMIN_PASSWORD=SEM_NAPIS_SVE_ADMIN_HESLO
EASYFLEX_SECURE_COOKIES=false

CADDY_SITE_ADDRESS=:80
WEB_CONCURRENCY=2
WORKER_CONCURRENCY=2
JOB_STALE_AFTER_S=120
MAINTENANCE_INTERVAL_S=60
JOB_RETENTION_HOURS=336
BATCH_STORAGE_RETENTION_HOURS=168
BACKUP_RETENTION_DAYS=14

MAX_PDF_FILES_PER_BATCH=100
MAX_PDF_FILE_BYTES=26214400
MAX_BATCH_TOTAL_BYTES=262144000
MAX_BATCH_TOTAL_PAGES=500
MAX_TABLE_FILE_BYTES=33554432
EOF
```

Teď upravte jen admin heslo:

```bash
nano .env
```

Najděte řádek:

```bash
EASYFLEX_ADMIN_PASSWORD=SEM_NAPIS_SVE_ADMIN_HESLO
```

A nahraďte ho vlastním heslem.

Uložení v `nano`:
- `Ctrl + O`
- Enter
- `Ctrl + X`

## 6. První spuštění

Spusťte všechno:

```bash
cd ~/EasyFlex-WebApp
docker compose up -d --build
```

První build bude pár minut trvat.

Pak ověřte stav:

```bash
docker compose ps
```

Měli byste vidět služby:
- `easyflex-db`
- `easyflex-web`
- `easyflex-worker`
- `easyflex-caddy`

## 7. Jak zjistit adresu serveru

Zobrazte lokální IP adresu serveru:

```bash
hostname -I
```

Vezměte první IP adresu z výpisu. Například:

```text
192.168.1.120
```

Teď v prohlížeči na svém počítači otevřete:

```text
http://192.168.1.120
```

Měla by se otevřít přihlašovací stránka EasyFlexu.

Přihlášení:
- uživatel: `admin`
- heslo: to, které jste dali do `EASYFLEX_ADMIN_PASSWORD`

Poznámka:
- v tomto prvním lokálním režimu běží EasyFlex jen přes `http://`
- proto je v `.env` nastaveno `EASYFLEX_SECURE_COOKIES=false`
- jinak by login nefungoval, protože prohlížeč neposílá secure cookie přes obyčejné HTTP

## 8. Nastavení EasyFlexu po prvním přihlášení

Po přihlášení:
- otevřete `/settings`,
- nastavte OpenAI API key,
- nastavte ABRA přístupové údaje,
- uložte.

Pak už můžete nahrávat PDF a tabulky.

## 9. Ověření, že běží i worker

Worker je důležitý. Bez něj se dávky jen zařadí do fronty a nezpracují se.

Zkontrolujte log workeru:

```bash
cd ~/EasyFlex-WebApp
docker compose logs -f worker
```

Ukončení výpisu:

```bash
Ctrl + C
```

## 10. Restart po rebootu

V compose je nastavené `restart: unless-stopped`, takže po restartu serveru se kontejnery znovu spustí samy.

Docker služba se spouští po startu systému automaticky.

## 11. Jak EasyFlex aktualizovat

Když změníte kód nebo stáhnete novou verzi:

```bash
cd ~/EasyFlex-WebApp
bash scripts/selfhost/update_stack.sh
```

Skript nově schválně zastaví update, pokud najde lokální změny v tracked souborech. Tím se snižuje riziko nechtěného konfliktu při `git pull`.

## 12. Jak udělat zálohu databáze

Spusťte:

```bash
cd ~/EasyFlex-WebApp
bash scripts/selfhost/backup_postgres.sh
```

Záloha se uloží do:

```text
~/EasyFlex-WebApp/backups/
```

Každý dokončený snapshot je adresář `easyflex_snapshot_<UTC čas>` obsahující:
- `database.dump` (PostgreSQL custom dump),
- `instance.tar.gz` (nahrané soubory),
- `environment.env` (tajná konfigurace),
- `revision.txt` a `SHA256SUMS`.

Adresáře mají oprávnění 700 a soubory 600. Snapshoty obsahují citlivá data;
neukládejte je do Gitu ani veřejného úložiště. Skript brání souběhu pomocí
`flock` a zveřejní snapshot až po ověření všech částí. Uchovává 14 dní
(`BACKUP_RETENTION_DAYS`). Starší samostatné SQL zálohy nemění.

Na Balleru je projekt v `/srv/projects/easyflex-webapp`. Uživatelský systemd
timer spouští zálohu denně v 03:15 se zpožděním do 10 minut a dožene
vynechaný běh. Při instalaci na jinou cestu upravte cesty v service souboru.

```bash
mkdir -p ~/.config/systemd/user
cp scripts/selfhost/systemd/easyflex-backup.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now easyflex-backup.timer
systemctl --user start easyflex-backup.service
systemctl --user list-timers easyflex-backup.timer
journalctl --user -u easyflex-backup.service
```

Pro běh bez přihlášení musí být zapnutý linger (`loginctl enable-linger`).
Tyto zálohy jsou na stejném disku jako aplikace; nechrání před ztrátou serveru.
Databáze má konzistentní snapshot. Soubory se zálohují za běhu, takže pro
časově sladěnou obnovovací kopii před migrací zastavte worker a blokujte nové
uploady. Obnovu ověřujte v izolované databázi přes `pg_restore --no-owner
--no-privileges`; nikdy neověřujte obnovu přepsáním produkce.

## 12b. Co se teď udržuje automaticky

Worker nově sám průběžně:
- znovu zařazuje stuck joby po vypršení heartbeat,
- plánuje retry s backoffem,
- čistí staré záznamy dokončených jobů,
- a maže orphaned dávkové soubory podle retention.

## 12c. Health a readiness

Pro jednoduchý dohled jsou k dispozici endpointy:

```text
/healthz
/readyz
```

- `/healthz` slouží jako liveness check procesu webu
- `/readyz` navíc ověřuje dostupnost databáze a vrací `503`, pokud DB není připravená

## 13. Jak službu vypnout nebo znovu spustit

Zastavení:

```bash
cd ~/EasyFlex-WebApp
docker compose down
```

Znovuspuštění:

```bash
cd ~/EasyFlex-WebApp
docker compose up -d
```

## 14. Jak ji zpřístupnit z internetu přes doménu a HTTPS

Tohle už neumím udělat za vás přímo, protože:
- musíte kliknout v routeru,
- a případně nastavit doménu / DNS.

Ale postup je jednoduchý.

### Varianta A: máte vlastní doménu

1. Nastavte DNS záznam `A` na vaši veřejnou IP adresu.
2. V routeru přesměrujte port `80` a `443` na lokální IP Ubuntu serveru.
3. Upravte `.env`:

```bash
cd ~/EasyFlex-WebApp
nano .env
```

Změňte:

```text
CADDY_SITE_ADDRESS=:80
```

na třeba:

```text
CADDY_SITE_ADDRESS=easyflex.mojedomena.cz
```

4. Uložte a restartujte stack:

```bash
docker compose up -d
```

Caddy si automaticky vyřídí HTTPS certifikát.
Po zapnutí HTTPS změňte v `.env` také:

```text
EASYFLEX_SECURE_COOKIES=true
```

### Varianta B: nemáte doménu

Použijte zdarma DuckDNS nebo jinou DDNS službu.

Nejjednodušší postup:
1. vytvořte si hostname, například `mojeeasyflex.duckdns.org`,
2. nastavte ho na svou veřejnou IP,
3. v routeru přesměrujte port `80` a `443` na server,
4. v `.env` nastavte:

```text
CADDY_SITE_ADDRESS=mojeeasyflex.duckdns.org
```

5. restartujte stack:

```bash
docker compose up -d
```

## 15. Co dělat, když stránka nejde otevřít

Základní diagnostika:

```bash
cd ~/EasyFlex-WebApp
docker compose ps
docker compose logs --tail=100 web
docker compose logs --tail=100 worker
docker compose logs --tail=100 caddy
docker compose logs --tail=100 db
```

## 16. Co běží uvnitř

- `db`: PostgreSQL databáze
- `web`: Flask + Gunicorn
- `worker`: zpracování fronty
- `caddy`: web server před aplikací

## 18. Varianta bez veřejné IP: Cloudflare Tunnel

Pokud jste za CGNAT a nechcete řešit veřejnou IPv4, použijte Cloudflare Tunnel.

Tohle je pro vás vhodné, když:
- doma vám web normálně běží,
- ale z internetu na něj nejde port forwarding,
- a máte doménu, kterou chcete použít například jako `easyflex.vvitovec.com`.

### Co je potřeba

- účet u Cloudflare,
- doména `vvitovec.com` přidaná do Cloudflare,
- a změněné nameservery u Forpsi na nameservery, které vám dá Cloudflare.

### 18.1 Přidejte doménu do Cloudflare

1. Přihlaste se do Cloudflare.
2. Klikněte na `Add a site`.
3. Zadejte `vvitovec.com`.
4. Dokončete přidání zóny.
5. Cloudflare vám ukáže 2 nameservery.

### 18.2 Přepište nameservery u Forpsi

Ve Forpsi:
1. otevřete správu domény `vvitovec.com`,
2. najděte nastavení nameserverů,
3. přepište původní nameservery na ty 2, které ukázal Cloudflare,
4. uložte změny.

Počkejte, až se změna propíše. Někdy je to za pár minut, někdy několik hodin.

### 18.3 Vytvořte tunnel v Cloudflare Zero Trust

1. V Cloudflare otevřete `Zero Trust`.
2. Jděte do `Networks` -> `Tunnels`.
3. Klikněte `Create a tunnel`.
4. Zadejte název, například `easyflex-home`.
5. Vyberte typ `Cloudflared`.
6. Vyberte prostředí `Docker`.

Cloudflare vám ukáže token nebo Docker command.

Zajímat vás bude jen samotný token.

### 18.4 Přidejte veřejný hostname

Ve stejném průvodci přidejte Public Hostname:

- `Subdomain`: `easyflex`
- `Domain`: `vvitovec.com`
- `Type`: `HTTP`
- `URL`: `web:10000`

To je správně, protože `cloudflared` běží ve stejné Docker síti jako EasyFlex web a umí se na službu `web` přímo připojit.

### 18.5 Uložte token do `.env`

Na Ubuntu otevřete:

```bash
cd ~/EasyFlex-WebApp
nano .env
```

Doplňte nebo upravte:

```text
EASYFLEX_SECURE_COOKIES=true
CLOUDFLARE_TUNNEL_TOKEN=SEM_VLOZ_TOKEN_Z_CLOUDFLARE
```

Poznámka:
- pro veřejnou Cloudflare doménu má být `EASYFLEX_SECURE_COOKIES=true`
- protože už půjde o HTTPS přístup z internetu

### 18.6 Spusťte tunnel container

Na Ubuntu spusťte:

```bash
cd ~/EasyFlex-WebApp
docker compose --profile cloudflare up -d
```

Tím se spustí i kontejner:
- `easyflex-cloudflared`

### 18.7 Ověřte, že tunnel běží

```bash
cd ~/EasyFlex-WebApp
docker compose ps
docker compose logs --tail=100 cloudflared
```

### 18.8 Otevřete EasyFlex z internetu

Pak otevřete:

```text
https://easyflex.vvitovec.com
```

Tohle už bude fungovat i bez veřejné IPv4 a bez port forwarding pravidel v routeru.

## 17. Důležité poznámky

- Pokud server vypnete, EasyFlex nebude dostupný.
- Pokud vypadne internet doma, EasyFlex nebude zvenku dostupný.
- Pokud chcete maximální spolehlivost, dělejte pravidelné zálohy.
- `.env` nikam neposílejte. Je v něm heslo do databáze i secret key.

## 17a. Jak převést stará data ze Supabase nebo Render PostgreSQL

Pokud už máte stará data v původní PostgreSQL databázi, můžete je přenést přímo.

Migrují se tabulky:
- `user`
- `user_settings`
- `company`
- `doc_type`
- `invoice_batch`
- `invoice_row`

Nemigrují se staré `batch_job` záznamy, protože ty jsou jen pracovní fronta a po přesunu nedávají smysl obnovovat.

### 17a.1 Odkud vzít starou PostgreSQL URL

Můžete použít jednu z těchto variant:

- stará `DATABASE_URL` z Renderu
- nebo PostgreSQL connection string ze Supabase `Project Settings -> Database -> Connection string -> URI`

Potřebujete plnou URL ve tvaru:

```text
postgresql://UZIVATEL:HESLO@HOST:5432/DATABAZE
```

### 17a.2 Udělejte si zálohu domácí databáze

```bash
cd ~/EasyFlex-WebApp
bash scripts/selfhost/backup_postgres.sh
```

### 17a.3 Nejdřív si ukažte plán migrace nanečisto

Níže místo `SEM_VLOZ_STAROU_DATABASE_URL` vložte svou starou PostgreSQL URL do apostrofů:

```bash
cd ~/EasyFlex-WebApp
docker compose exec -T -e SOURCE_DATABASE_URL='SEM_VLOZ_STAROU_DATABASE_URL' web python scripts/selfhost/migrate_from_postgres.py --dry-run
```

### 17a.4 Proveďte skutečnou migraci

```bash
cd ~/EasyFlex-WebApp
docker compose exec -T -e SOURCE_DATABASE_URL='SEM_VLOZ_STAROU_DATABASE_URL' web python scripts/selfhost/migrate_from_postgres.py --yes
```

### 17a.5 Po migraci restartujte stack

```bash
cd ~/EasyFlex-WebApp
docker compose restart web worker
```

### 17a.6 Co zkontrolovat po migraci

- přihlášení uživatelů
- firmy a typy dokladů v UI
- nastavení v `/settings`
- historie batchů a výsledků
