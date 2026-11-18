# tam: design

`tam` (tmux-agent-man) gjør det enkelt å starte agentsesjoner (Claude, Codex m.fl.) i tmux, og å få tilbake hele tmux-oppsettet, inkludert agentsamtalene, etter en reboot.

## Hva tam er

`tam` er lim mellom etablerte verktøy. Det kombinerer tmux, fzf, zoxide og agentenes egne hooks og resume-funksjoner slik at agentsesjoner er enkle å starte, holde oversikt over og hente tilbake etter en reboot. `tam` lager bare det verktøyene ikke dekker selv: koblingen mellom en tmux-pane og agentens sesjons-ID, lagring og gjenoppretting av tmux-oppsettet, og noen få popups som binder det sammen. README-en skal presentere `tam` på denne måten, og forklare hvert verktøy med 1–2 linjer.

## Mål

- Etter en reboot gjenoppretter `tam` alle tmux-sesjoner, vinduer og panes med layout og mappe. Agentpanes gjenopptar samme samtale.
- Fungerer for tmux-brukere som allerede har eget oppsett, og endrer ikke smaksinnstillinger.
- Tar tydelige valg og har få konfigurasjonsmuligheter.
- Er ikke låst til Claude. Nye agenter legges til i konfigurasjonen uten kodeendringer. Alle Claude-funksjoner fungerer i panene.

## Utenfor v1

- Git worktrees. Agentens egne flagg sendes gjennom med `--`.
- Gjenoppretting av programmer i shell-panes (`vim` o.l.) og av scrollback.
- `adopt` for andre agenter enn Claude.
- Automatisk restore ved innlogging eller når iTerm åpnes.

## Komponenter

| Komponent | Oppgave |
|---|---|
| `tam` (Python 3.11+, bare standardbiblioteket) | CLI, `snapshot`, restore, `run`-wrapper og hook-mottak |
| `tam tmux-init` | Setter hooks som kaller `tam snapshot`, tastebindinger og tmux-innstillingene Claude trenger. Kalles fra `~/.tmux.conf` med `run-shell "tam tmux-init"`, slik at det ikke finnes en konfigurasjonsfil som må holdes i takt med versjonen. |
| Claude `SessionStart`-hook | Kaller `tam hook claude` med hook-JSON på stdin |
| Agentdefinisjoner (TOML) | Innebygd `claude`, `codex` og `shell`. Egne agenter og overstyringer ligger i `~/.config/tam/harnesses.toml`. |
| `~/.local/state/tam/state.json` | Lagret bilde av tmux-serveren. Forrige versjon beholdes som `state.prev.json`. |
| `~/.local/state/tam/seen/<pid>` | Claude-sesjoner registrert av hooken, brukt av `adopt` |

Avhengigheter: `tmux` 3.3+, `fzf` (mappevelger og `adopt`), `python@3.13`. Valgfritt: `zoxide`, som gir flere mapper i velgeren. Verktøyene står i `src/tam/Brewfile`, som følger med pakken. `tam setup` viser status for hvert verktøy og tilbyr `brew bundle install` for det som mangler. Når `tam` blir en brew-formel, gjør `depends_on` det samme for de påkrevde verktøyene. Ingen tmux-plugins, ingen TPM, ingen tmux-resurrect eller tmux-continuum.

## Agentdefinisjon

```toml
[harness.claude]
start  = "claude -n {name}"
resume = "claude --resume {id}"

[harness.codex]
start  = "codex"
resume = "codex resume --last"
```

Panes uten agent er vanlige shell-panes og trenger ingen definisjon.

Innebygde agenter: `claude`, `codex`, `gemini`, `qwen`, `opencode`, `crush`, `copilot`, `cursor` (`cursor-agent`), `goose`, `aider` og `amp`. En agent regnes som installert når programmet i `start` finnes i `PATH`. Resume-kommandoene uten `{id}` fortsetter siste sesjon i panens mappe. Amp har bare resume med tråd-ID og starter derfor en ny tråd etter restore. Hver innebygde agent testes gjennom start, avslutning og resume med en falsk binær (`tests/test_harnesses.py`).

Standardagenten, som `⌥ a`, `prefix + A` og `tam new` uten agentnavn bruker, står i `~/.config/tam/config.toml` (`default_agent`). `tam setup` foreslår en standard første gang: `claude` hvis den finnes, deretter `codex`, deretter den første som er installert. Finnes flere, spør `tam setup`. `tam agent <navn>` endrer standarden, og `tam agent` lister installerte agenter. I mappevelgeren bytter `Tab` agent for akkurat denne starten.

- Inneholder `resume` `{id}`, hentes ID-en fra pane-optionen `@tam_session_id`. Den settes av agentens hook.
- Uten `{id}` kjøres `resume` som den står. For Codex betyr det siste samtale i mappen, så to Codex-panes i samme mappe gjenopptar samme samtale. Det er en kjent begrensning.
- Uten `resume` startes agenten på nytt med `start`.
- Agenter som har hooks, kan rapportere ID med `tam hook session <id>`.

## Tilstand

Pane-options satt av `tam`:

| Option | Innhold |
|---|---|
| `@tam_harness` | Agentnavn fra TOML |
| `@tam_name` | Navnet som sendes til agenten (`{name}`) |
| `@tam_args` | Ekstra agentflagg fra `tam new ... -- flagg`, som JSON |
| `@tam_session_id` | Agentens sesjons-ID, satt av hooken |
| `@tam_started` | Satt når agenten har vært startet i panen. Avgjør om en `resume` uten `{id}` brukes. |

Alle verdiene sendes som argumenter til `tam run`, og `tam run` setter optionene selv. Det unngår kappløp mellom oppretting av panen og setting av optioner, og panene trenger ingen egen nøkkel.

`state.json` inneholder sesjoner (navn), vinduer (indeks, navn, `window_layout`, aktiv, `automatic-rename`) og panes (indeks, mappe, aktiv, agent). `last_session` er sesjonen som sist var attached. tmux 3.8 rapporterer `window_layout` som JSON, eldre versjoner som `csum,WxH,...`. `select-layout` godtar begge.

## Flyt

### Opprette

`tam new [agent] [-n navn] [--split h|v] [-- agentflagg]`

1. Prosjektnavnet er navnet på git-roten. Utenfor git brukes navnet på mappen.
2. Uten `--split` lages et nytt vindu `<prosjekt>:<navn>` med automatisk omdøping av. Navnet er som standard agenten med løpenummer (`api:claude`, `api:claude-2`).
3. Panen kjører `tam run --harness <agent> --name <navn> -- <flagg>`.

### Lagre

`tam tmux-init` registrerer hooks for `after-new-window`, `after-split-window`, `after-kill-pane`, `pane-exited`, `window-renamed`, `window-layout-changed`, `session-window-changed`, `window-pane-changed`, `session-created`, `session-renamed`, `session-closed` og `client-session-changed`. Hver hook kjører `run-shell -b "tam snapshot"`. Hookene settes på indeks 42 (`after-new-window[42]`), slik at hooks brukeren har satt selv ikke overskrives.

### Shutdown

macOS sender SIGTERM til alle prosesser ved shutdown, ikke bare til tmux-serveren. Panes som lukkes før serveren avslutter, ville ellers blitt fjernet fra tilstanden. To tiltak hindrer det:
- `tam run` ignorerer SIGTERM. Agenten avslutter, men panen blir stående.
- Hooks for lukking (`pane-exited`, `after-kill-pane`, `session-closed`) venter 2 sekunder før de lagrer. Er serveren borte da, skrives ingenting.

Begge er dekket av `test_shutdown_sigterm_to_everything_keeps_agent_panes`.

`tam snapshot`:
1. Gjør ingenting hvis serveroptionen `@tam_restored` ikke er satt. Det hindrer at en ny, tom tmux-server overskriver tilstanden før restore.
2. Leser `list-sessions`, `list-windows -a` og `list-panes -a` med pane-options.
3. Flytter `state.json` til `state.prev.json` og skriver ny `state.json` via en midlertidig fil og `rename`.

En `cd` i et shell lagres ved neste tmux-hendelse. Skjer det en reboot før neste hendelse, går mappeendringen tapt. Det er akseptert.

### Gjenopprette

`tam`:
1. Starter tmux-serveren hvis den ikke kjører.
2. Hvis `@tam_restored` ikke er satt: for hver sesjon i `state.json` som ikke finnes, lages sesjonen, vinduene og panene. Deretter kjøres `select-layout <window_layout>`, og pane-options settes. Sesjoner som allerede finnes, røres ikke. Til slutt settes `@tam_restored 1`.
3. Shell-panes starter et shell i lagret mappe. Agentpanes kjører `tam run --wait` med de lagrede verdiene.
4. Attacher til sesjonen for prosjektet i gjeldende mappe (git-roten, ellers mappen), og lager den om den mangler. Fra `~` attacher `tam` til sist aktive sesjon. Har `! tam adopt` satt `@tam_attach_to`, brukes den sesjonen.

Finnes ikke mappen lenger, starter panen i `$HOME` med en advarsel.

### Mappevelger

`prefix + a` kjører `tam new claude --popup-client ... --target ... --dir ...` via `run-shell`, som ekspanderer tmux-formatene. `tam` åpner så en popup med `tam new --pick` og ferdig utfylte verdier, fordi `display-popup` ikke ekspanderer formater i kommandoen. Popupen tilpasses klientens størrelse, fordi tmux uten feilmelding avviser en popup som er større enn klienten.

Listen i velgeren, uten duplikater og bare mapper som finnes:
1. gjeldende mappe (Enter alene gir samme oppførsel som før velgeren fantes),
2. mapper Claude har kjørt i, fra `cwd` i det nyeste transkriptet per prosjekt i `~/.claude/projects`, sist brukt først,
3. mapper fra panene i `state.json`,
4. `zoxide query -l`, hvis `zoxide` er installert.

Er det du skriver ikke i listen, men er en mappe som finnes, brukes den. `tam new -C <mappe>` gjør det samme fra kommandolinjen.

### `tam run`

Wrapperen som kjører i alle agentpanes:

- Med `--wait` vises `Enter = gjenoppta, q = lukk` før agenten startes. Det gir lat restore.
- Uten `--wait` startes agenten med `start`, eller med `resume` hvis panen har en sesjons-ID.
- Når agenten avslutter, vises `Enter = gjenoppta, q = lukk`.
- Feiler resume (ikke-null exit innen 10 sekunder), vises feilen og `Enter = ny sesjon, q = lukk`. Det skjer blant annet for en Claude-sesjon der ingen melding ble sendt, fordi Claude ikke lagrer transkript før første melding.

### Claude-hook

`tam setup` legger inn en `SessionStart`-hook i `~/.claude/settings.json`. `tam hook claude` leser `session_id` og `cwd` fra stdin og gjør to ting:
- Skriver `seen/<claude-pid>` med pid, tty, mappe og sesjons-ID. Dette gjøres for alle Claude-sesjoner.
- Hvis `$TMUX_PANE` er satt og panen har `@tam_harness`, settes `@tam_session_id` på panen. Det holder koblingen riktig etter `/clear` og `/resume`.

### Agentstatus og sidebar

Pane-optionen `@tam_status` har en av disse verdiene:

| Status | Satt av |
|---|---|
| `running` | `tam run` når agenten startes (agenter uten status-hooks blir stående her) |
| `working` | Claude-hooks `UserPromptSubmit` og `PostToolUse` |
| `waiting` | Claude-hooks `SessionStart`, `Stop` og `Notification` (venter på deg) |
| `exited` | `tam run` når agenten avslutter |
| `stopped` | `tam run --wait` etter restore |

Status-hookene er rene shell-kommandoer (`tmux set -p -t "$TMUX_PANE" @tam_status ...`) uten Python, fordi `PostToolUse` kjører ved hvert verktøykall. Statusen lagres ikke i `state.json`.

Sidebaren er en pane på 44 kolonner lengst til venstre i hvert vindu. Den kjører `tam sidebar --pane` (`fzf`) og gjenkjennes på startkommandoen. Den viser sesjoner og vinduer. Hvert vindu er et kort, og kortet peker på vinduet med vindus-ID-en (`@12`), som ikke endres når vinduer legges til eller flyttes:

1. Navn. For en agent er det pane-tittelen agenten setter (Claude oppdaterer den når emnet endrer seg), med mindre vinduet har fått nytt navn med `Ctrl+r` (vindus-optionen `@tam_named`, lagret i `state.json`). En tittel som bare er programnavnet, regnes ikke som emne. Et vindu som heter det samme som skallet (`zsh`, `bash`, ...) og har automatic-rename på, vises med mappenavnet.
2. Hva som kjører. For en agent er det agentnavnet og mappen. For en terminal er det kommandolinjen til programmet i forgrunnen (fra ett `ps`-kall per liste), eller mappen når skallet venter.
3. Status, antall panes (når det er flere enn én), tid siden aktivitet og git-branch.
4. Lenker, når det finnes: `Issue: #40, #41  PR: #12`, opptil tre åpne issues og PR-er, understreket og som OSC 8-lenker (Cmd+klikk åpner). Først branchens åpne PR (`gh pr list --head`), så henvisninger i agentens tittel (`<repo> sak <nr>`, `<repo> PR <nr>`, `issue <nr>`, `repo#<nr>`), så GitHub-lenkene i de siste 3000 linjene av agentpanen. En henvisning i tittelen slås opp i repoet med det navnet hos eierne av GitHub-remotene i tam sine panes, eller uten navn i agentens eget repo. Status og type kommer fra `gh api repos/…/issues/<n>` (også PR-er), og bare `open` vises. En henvisning som står på lenkelinjen, fjernes fra navnet på kortet. Begge kjøres i en bakgrunnsprosess og caches i `~/.local/state/tam/pr/` og `~/.local/state/tam/refs/` (skann høyst hvert 30. sekund, status i 5 minutter). `tmux-init` legger til `terminal-features '*:hyperlinks'`.

En pane er en agent når tam startet den (`@tam_harness`), eller når programmet til en kjent agent kjører i forgrunnen. Da får også en `claude` startet for hånd et agentkort, med statusen hookene setter. Den lagres ikke for restore. Det gjør `tam adopt`.

Et vindu med flere agentpanes viser den mest presserende statusen (`waiting` før `working`). Vinduet sidebaren står i (det aktive) har en farget strek og farget navn. Markøren (`fzf` sin markerte linje) flyttes med piltastene og er uavhengig av det aktive vinduet. Et klikk på et kort, eller Enter, kjører `switch-client` til vinduet. Linjen for sidebarens eget vindu flytter fokus tilbake til vinduet, og det gjør også Esc. `Ctrl+r` gir nytt navn til linjen. `⌥ w` lukker vinduet som er markert, etter `confirm-before`. Et klikk på `+ terminal` i en sesjonslinje åpner et skall i et nytt vindu rett etter agentvinduet, i agentens mappe. Agenten er den i sesjonens gjeldende vindu, ellers den første i sesjonen.

`fzf` oppgir hvilken linje et klikk treffer, men ikke kolonnen. tam binder derfor `MouseDown1Pane` i root-tabellen. I en sidebar sendes klikket videre til `fzf` (`send-keys -M`), og `tam sidebar --mouse` kjøres med ordet og linjen under musen (`#{mouse_word}`, `#{mouse_line}`). Er ordet `+` eller `terminal`, leser tam sesjonen fra linjen `fzf` har markert, over socketen. Andre panes beholder tmux sin standardbinding, og en binding brukeren har satt selv, erstattes ikke.

Hver sidebar sin `fzf` lytter på en unix-socket (`--listen`) ved siden av tmux sin socket, i en katalog bare brukeren har tilgang til. tmux-hooks på indeks 44 (`session-window-changed`, `client-session-changed`) kjører `tam sidebar --refocus`, som sender `clear-query+pos(n)` til sidebaren i vinduet som ble aktivt, slik at den markerer sitt eget kort. `⌥ w` leser den markerte linjen med `GET`.

Sidebaren vises eller skjules i alle vinduer samtidig: `sidebar = "shown"` eller `"hidden"` i `config.toml`, speilet i tmux-optionen `@tam_sidebar_shown`. Standard er `shown`. tmux-hooks på indeks 43 (`after-new-window`, `session-created`, `client-resized`, `window-resized`, `pane-exited`, `after-kill-pane`) kjører `tam sidebar --ensure` når optionen og `@tam_restored` er satt. `--ensure` legger sidebaren til der den mangler, setter bredden tilbake til 44, fjerner den når den er skjult, og lukker et vindu der bare sidebaren er igjen. Etter lukking venter den to sekunder, som snapshot. Ellers ville vinduer kunne lagres som borte under en shutdown. Sidebaren lagres i `state.json` som en pane med `"sidebar": true`, slik at layouten passer ved restore. Etter restore kjøres `--ensure`.

Hvert 2. sekund bygger en synlig sidebar lista i bakgrunnen (`fzf` `every(2)` med `bg-transform`, `tam sidebar --tick`), og laster inn på nytt bare når lista eller headeren er endret. Lista leses da fra en fil i `~/.local/state/tam/sidebar/`. `transform` er synkron i `fzf`, og en reload på omtrent 240 ms hvert 2. sekund gjorde at klikk gikk tapt. `--track --id-nth 1` holder markøren på samme linje. En sidebar i et vindu ingen klient viser, bruker ett tmux-kall per tick og bygger ingen liste. `tam tmux-init` starter alle sidebarer på nytt (`respawn-pane -k`), fordi `fzf` leser bindingene sine bare ved oppstart. Mens sidebaren har fokus, beholder vinduet navnet sitt. Det gjøres med en `automatic-rename-format` per vindu som leser pane-optionen `@tam_sidebar`.

`Ctrl+x` i sidebaren lukker linjen som er markert, i to trykk: det første setter bunnfeltet til `✕ Ctrl+x again to close ...`, og det andre lukker med `kill-window` eller `kill-session`. Hvis du flytter til en annen linje, nullstilles det. Den aktiverte linjen lagres i `~/.local/state/tam/sidebar-armed`, fordi hver `fzf`-handling kjører i en ny prosess. Lukkes sesjonen klienten står i, flyttes klienten først til en annen sesjon, ellers ville tmux koblet den fra.

### `tam adopt`

1. Viser en `fzf`-liste. Øverst står Claude-prosesser som kjører utenfor `tam`, med mappe, tty, navn og første melding. Under står nylig avsluttede samtaler fra `~/.claude/projects`.
2. For en prosess som kjører: `tam` ber om bekreftelse, sender SIGTERM og venter til prosessen er avsluttet. Et svar Claude er midt i, går tapt.
3. `tam` lager en pane som gjenopptar samtalen.
4. `seen`-filer for prosesser som ikke lenger kjører, slettes.

Kjøres `tam adopt` fra en Claude-sesjon (`! tam adopt`), er Claude en forelderprosess. Da flyttes den sesjonen uten liste og uten bekreftelse. `tam adopt` starter en hjelpeprosess i en egen prosessgruppe (`start_new_session`), skriver en melding som Claude viser, og avslutter. Hjelpeprosessen venter ett sekund, stopper Claude, gjenoppretter eller starter tmux, åpner vinduet med `--resume` og setter `@tam_attach_to`, slik at neste `tam` attacher til den sesjonen. Feil skrives til `~/.local/state/tam/adopt.log`.

Sesjons-ID for en Claude-prosess som kjører, hentes i denne rekkefølgen:

1. Claude Codes egen sesjonsfil `~/.claude/sessions/<pid>.json`. Nyere versjoner skriver den, med `sessionId`, `cwd`, `name` og `status`. Lista viser da Claudes navn og status.
2. tam sin `seen`-fil fra hooken.
3. `lsof` (mappe) og transkriptet som sist ble endret. Har du flere sesjoner i samme mappe, er det et gjett.

Formatet på sesjonsfilen er ikke dokumentert. Filer som ikke kan leses, eller som mangler felt, hoppes over. Uten filene virker `tam adopt` som før.

## CLI

| Kommando | Funksjon |
|---|---|
| `tam` | Restore ved behov, deretter attach |
| `tam new [agent] [-n navn] [--split h\|v] [-- flagg]` | Ny agentpane |
| `tam adopt` | Meld inn en ekstern eller avsluttet Claude-sesjon |
| `tam move [sesjon]` | Flytt gjeldende vindu til en annen sesjon |
| `tam sidebar` | Flytt fokus til sidebaren, eller tilbake. `--toggle` viser eller skjuler den i alle vinduer. |
| `tam mcp` | MCP-server over stdio, startet av agentene. `install` / `uninstall` registrerer den hos alle installerte agenter. |
| `tam --version` | Versjonen. Fra en git-checkout: `<release>+dev.<commit>`, med `.dirty` når sporede filer er endret. |
| `tam help` | Jukselapp med `tam`- og tmux-versjon: popup inne i tmux, ellers utskrift |
| `tam setup` | Legg til `source`-linjen i `~/.tmux.conf`, installer Claude-hooken og sjekk avhengigheter |
| `tam snapshot`, `tam run`, `tam hook`, `tam tmux-init` | Interne kommandoer, kalles av tmux, panes og hooks |

En pane lukkes med vanlig tmux (`exit`, `kill-pane` eller `q` i `tam run`). Hooken fjerner den fra tilstanden.

## tmux-oppsett fra `tam tmux-init`

Taster:

| Tast | Funksjon |
|---|---|
| `prefix + a`, `⌥ a` | Velg mappe i en `fzf`-popup og start standardagenten der: Enter i nytt vindu, `Ctrl+v` split til høyre, `Ctrl+s` split under |
| `prefix + t`, `⌥ t` | Samme velger for en vanlig terminal uten agent (`tam term`). Erstatter tmux sin `clock-mode` på `t`. |
| `prefix + A` | `tam new --split h`: standardagenten i en split |
| `prefix + h`, `⌥ h` | Jukselappen i en popup (`tam help`) |
| `prefix + .` | Flytt vinduet til en annen sesjon, valgt i en `fzf`-popup (`tam move`). Et nytt navn lager sesjonen. Flyttes det siste vinduet ut av en sesjon, følger klienten med. Erstatter tmux sin standard `move-window`-prompt. |
| `prefix + s`, `⌥ s` | Fokus til sidebaren, og tilbake (`tam sidebar`). En skjult sidebar vises først. Erstatter tmux sin standard `choose-tree -Zs`. |
| `prefix + S`, `⌥ S` | Vis eller skjul sidebaren i alle vinduer (`tam sidebar --toggle`) |
| `⌥ q` | `detach-client`, som `prefix + d`. tmux og agentene kjører videre. |
| `⌥ w` | Lukk vinduet som er markert i sidebaren, etter `confirm-before` (`tam sidebar --close-marked`). Uten sidebar: gjeldende vindu, som `prefix + &`. |
| `⌥ /` | tmux sin `command-prompt`. Teksten kjøres som `tam <tekst>` i en popup (`tam prompt`), som venter på en tast til slutt. |
| Klikk (`MouseDown1Pane`) | I en sidebar: sendes videre til `fzf`, og `+ terminal` åpner en terminal. Ellers tmux sin standard. |

`⌥ a`, `⌥ s`, `⌥ S`, `⌥ h`, `⌥ q`, `⌥ w` og `⌥ /` ligger i root-tabellen (uten prefix) og virker når terminalen sender Option som Meta (iTerm2: Left Option key = Esc+). Tastene får en beskrivelse (`-N "tam: ..."`), så de vises i `prefix + ?`. En tast med tmux sin standardbinding (`s`) erstattes. En tast brukeren selv har bundet til noe annet (f.eks. `h` for vim-navigasjon), overskrives ikke. Jukselappen viser da kommandoen i stedet for tasten.

Hver gang `tam` attacher, vises jukselappen som popup. Den lukkes med en vilkårlig tast. Utenfor tmux gjøres det med en engangs-hook på `client-attached`, siden klienten ikke finnes før `tam` har gjort `exec tmux attach`. Popupen tilpasses klientens størrelse (`tam help --client`), fordi tmux uten feilmelding avviser en popup som er større enn terminalen.

`prefix + f` er fjernet. Den skulle åpne `choose-tree -Zw`, og det gjør tmux sin standardbinding `prefix + w` allerede.

Innstillinger Claude trenger: `extended-keys on`, `terminal-features 'xterm*:extkeys'`, `allow-passthrough on`, `set-clipboard on`, `focus-events on`.

`tam` endrer ikke prefix, `mouse`, `history-limit`, `base-index` eller statuslinjen.

## zsh

Brew installerer `_tam`-completion i `share/zsh/site-functions`. Den fullfører underkommandoer og agentnavn fra TOML. `tam` endrer ikke `.zshrc`.

## Distribusjon

- Offentlig GitHub-repo `tam` med koden.
- Offentlig repo `homebrew-tap` med `Formula/tam.rb`. Installasjon er `brew install <bruker>/tap/tam` etterfulgt av `tam setup`.
- Før tap-en finnes: `uv tool install git+https://github.com/<bruker>/tam`.
- Utvikling: `uv run pytest`, `ruff`. Ingen runtime-avhengigheter.

## README

Forklarer hvilket problem `tam` løser, hvilke verktøy som er involvert (tmux, agent-CLI-ene, Claude-hooken og fzf) og flyten opprett, lagre, reboot, gjenopprett. Flyten vises som Mermaid-diagrammer.
