# Bet Agent — denní tipy na Telegram

Python agent, který jednou denně projde nabídku tenisu, basketbalu a házené,
spočítá **skutečnou šanci favorita na výhru**, rozdělí zápasy do tří
kurzových pásem a z každého pošle na Telegram tři nejjistější zápasy dne i noci.

Agent **neřeší peníze** — žádný bankroll, žádná výše vkladu, žádná kampaň.
Jen tipy; kolik a jestli vůbec vsadíš, je na tobě.

```
🎯 TIPY NA 15.09.

📋 Minulé tipy
🟢 A: ✅ Sinner
🔵 C: ❌ Djokovic

🟢 PÁSMO A · kurz 1,15–1,19
1. Carlos Alcaraz — 86,0 % · kurz 1,15 (max 1,16)
    🎾 ATP Peking · dnes 11:30 · proti Kvalifikant
📊 12/14 (86 %) · čekáno 84 % · virtuálně +85 Kč

🟠 PÁSMO B · kurz 1,20–1,24
1. Boston Celtics — 81,1 % · kurz 1,21 (max 1,24)
    🏀 NBA · zítra 01:30 · proti Washington Wizards
📊 zatím bez vyhodnocených tipů (čeká 3)

🔵 PÁSMO C · kurz 1,25 a výš
1. SC Magdeburg — 76,9 % · kurz 1,28 (max 1,29)
    🤾 Handball-Bundesliga · dnes 19:00 · proti HSG Wetzlar · remíza = prohra
📊 5/7 (71 %) · čekáno 75 % · virtuálně −12 Kč
```

---

## Kurzová pásma

| Pásmo | Kurz | K 2× potřebuješ zhruba |
|---|---|---|
| 🟢 **A** | 1,15–1,19 | 5 výher v řadě (od kurzu 1,19 stačí 4) |
| 🟠 **B** | 1,20–1,24 | 4 výhry v řadě |
| 🔵 **C** | 1,25 a výš | 3–4 výhry v řadě (3 stačí od průměrného kurzu 1,26) |

- **Zařazení podle běžného kurzu** — tedy mediánu napříč kancelářemi, zaokrouhleného
  na setiny. Nejlepší kurz často nabízí burza (Betfair, Matchbook) a české kanceláře
  v datech nejsou, takže u své kanceláře dostaneš spíš kurz kolem mediánu.
  Nejlepší kurz je ve zprávě v závorce jako `max`.
- **V každém pásmu tři nejvyšší šance.** Protože vyšší šance = nižší kurz, tipy se
  drží spíš u spodní hranice pásma.
- **Pásmo C nemá horní hranici**, ale protože se berou nejvyšší šance, tipy v něm
  budou v praxi hlavně kolem 1,25–1,35.
- **Tichý zbytek** — zápasy s kurzem pod 1,15 se neposílají, ale ukládají se se
  stejnými statistikami. Za pár měsíců tak půjde porovnat, jestli by si vedly
  lépe (`python main.py --status`).
- **Změna pásem je zpětná** — pásmo se u každého tipu počítá z uloženého kurzu,
  takže po úpravě `ODDS_BANDS` se celá historie roztřídí podle nových pásem.

Proč zrovna střední kurzy: simulace s reálnými maržemi ukázala, že pro sérii
all-in sázek k 2× vychází nejlépe pásmo zhruba 1,11–1,35. Příliš krátké kurzy
znamenají zaplatit marži mnohokrát, dlouhé kurzy mají marži vyšší. Bez náskoku
proti trhu ale žádné pásmo nepřesáhne ~40–45 % šance na 2×.

### Statistiky pásma

| Údaj | Význam |
|---|---|
| **12/14 (86 %)** | skutečná úspěšnost vyhodnocených tipů; vrácené vklady se nepočítají |
| **čekáno 84 %** | jakou úspěšnost agent předpovídal. Když sedí se skutečností, výpočty fungují. |
| **virtuálně +85 Kč** | zisk, kdybys na každý tip dal 100 Kč (počítáno s běžným kurzem). Jen společné měřítko pásem, ne doporučení vkladu. |

Úspěšnost se dá hrubě číst po pár týdnech, virtuální zisk až po několika měsících —
jedna prohra v pásmu A smaže zisk z mnoha výher. Starší tipy z doby před pásmy
jsou zařazené podle tehdy uloženého nejlepšího kurzu.

---

## Co znamenají čísla u tipu

| Údaj | Význam |
|---|---|
| **Šance** | Pravděpodobnost výhry po odstranění marže sázkovky. Podle ní jsou tipy v pásmu seřazené. |
| **Kurz** | Běžný kurz (medián kanceláří), podle kterého se tip zařadil do pásma. |
| **max** | Nejlepší nalezený kurz, často z burzy. |
| **Všechny najednou** | Pravděpodobnost, že vyjdou všechny tipy pásma. |
| **remíza = prohra** | Házená: tip zní „favorit vyhraje", remíza je prohraná sázka. |

### Jak se šance počítá

1. **Odstranění marže** — kurz 1,25 neznamená 80 %, protože je v něm zisk
   sázkovky. Agent marži odstraní Shinovým modelem, který ji ubírá víc
   outsiderům (odpovídá tomu, jak se trh chová doopravdy).
2. **Konsensus** — udělá se to pro každou sázkovku zvlášť a vezme se **medián**.
   Jedna utržená nabídka tak výsledek nerozhodí.
3. **Kontrola důvěryhodnosti** — zápas vypadne, jen když je kanceláří málo
   (< 4), moc se rozcházejí (> 4 p.b.), nebo do začátku zbývá málo času.
4. **Pásma a seřazení** — zápas se zařadí podle běžného kurzu a v každém pásmu
   se pošlou tři s nejvyšší šancí.

### Jednu věc je fér vědět

Vysoká úspěšnost tipů **neznamená automaticky zisk**. V každém kurzu je marže
sázkovky, takže samotné sázení na favority má dlouhodobě zápornou očekávanou
hodnotu — i když vyhrají 19 z 20 zápasů. Agent hledá jistotu, ne výhodnost.
Kdyby ti šlo o dlouhodobý zisk, potřeboval bys hledat kurzy vyšší, než
odpovídá skutečné šanci; to je jiná úloha než tahle.

Sázej jen peníze, které můžeš ztratit. Linka pomoci: **800 222 222**
(Národní linka pro odvykání, ČR).

---

## Proč zrovna tyhle sporty

| Sport | Trh | Poznámka |
|---|---|---|
| **Tenis** | dvoucestný | Žádná remíza. V raných kolech turnajů bývají favorité přes 90 %. Při skreči většina kanceláří sázku ruší. |
| **Basketbal** | dvoucestný | Z týmových sportů nejmíň náhodný — hodně skórování znamená, že lepší tým vyhrává častěji. |
| **Házená** | **trojcestný** | Remíza je možná (bývá 3–8 %) a je to **prohraná sázka**. Spočtená šance to už zohledňuje, tip je ve zprávě označený. |
| Baseball, hokej | dvoucestný | Jen v profilu `wide`. Výrazné favority mají vzácněji. |
| Fotbal | 1X / DNB | Vypnutý — remíza vyžaduje odvozené trhy, které se dotahují po jednotlivých zápasech a stojí 2 kredity za zápas. Zapíná se přes `ENABLE_SOCCER`. |

Profily: `individual` (jen tenis), `low_variance` (tenis + basketbal),
**`core` (výchozí: tenis + basketbal + házená)**, `wide` (+ baseball a hokej).

### Sporty s remízou

U házené kotují kanceláře trojcestný trh (výhra / remíza / prohra). Tip
„výhra favorita" tedy **remízou prohrává** — proto je u něj ve zprávě řádek
`výhra favorita (remíza = prohra)`. Šance je o pravděpodobnost remízy nižší,
což je správně: je to opravdu méně jistá sázka než stejně vysoký kurz v tenise.

Občas se stane, že část kanceláří kotuje dvoucestně a část trojcestně (typicky
hokej: se zahrnutím prodloužení vs. jen základní hrací doba). To jsou **různé
sázky** a míchat je do jednoho mediánu by nedávalo smysl — agent proto vezme
jen převažující strukturu a zbytek zahodí.

**Sezónnost je zásadní.** Nejlepší nabídka je od října do dubna, kdy běží NBA,
Euroliga i házenkářská Bundesliga a tenisové turnaje mají raná kola. V září
(konec US Open, basketbal ještě nezačal) je nabídka nejslabší v roce a šance
kolem 70–80 % jsou maximum.

---

## Rozpočet kreditů

Free tier The Odds API dává **500 kreditů měsíčně**. Cenu určuje počet soutěží,
které zrovna hrají — **soutěže mimo sezónu jsou zdarma**.

| Krok | Kredity |
|---|---|
| Výpis soutěží | 0 |
| Kurzy (1 dotaz na soutěž se zápasy) | 1 za soutěž |
| Dohledání výsledků | 2 za soutěž, jen když jsou nevyhodnocené tipy |
| **Reálně** | **~5–15 za den** |

Pojistka: pod `MIN_CREDITS_RESERVE` (40) už agent kurzy nestahuje a pošle
varování. Skutečnou spotřebu vypisuje každý běh do `agent.log`.

Levnější tarif než free neexistuje — další je až $30/měsíc za 20 000 kreditů.

---

## Architektura

```
main.py         denní běh: výsledky → nabídka → tipy → Telegram
├── config.py   načtení nastavení, sportovní profily, pásma, validace
├── fetcher.py  The Odds API: výběr soutěží, kurzy, výsledky
├── analyzer.py odstranění marže, konsensus, kontroly, pásma
├── notifier.py sestavení a odeslání zprávy (MarkdownV2)
└── state.py    deník tipů a statistiky pásem

settings.env        běžné nastavení (v repozitáři, upravitelné na GitHubu)
.env                tajné klíče jen pro lokální běh (NIKDY na GitHub)
tips.json           deník tipů (v repozitáři, GitHub do něj zapisuje)
.github/workflows/denni-tipy.yml   plán běhu na GitHubu
selftest.py         offline kontrola všeho výše (bez API klíče)
telegram_setup.py   jednorázové propojení s Telegramem
```

---

## Instalace a spuštění

```powershell
cd C:\Users\petr.shromazdil\Desktop\bet-agent
pip install -r requirements.txt
copy .env.example .env      # doplň ODDS_API_KEY a TELEGRAM_BOT_TOKEN (ostatní je v settings.env)
python telegram_setup.py    # najde chat ID a pošle testovací zprávu
python selftest.py          # kontrola bez API klíče
python main.py --dry-run    # zkouška naostro, ale bez odeslání
python main.py              # ostrý běh
```

| Příkaz | Co dělá |
|---|---|
| `python main.py` | denní běh: výsledky + nové tipy na Telegram |
| `python main.py --dry-run` | nic neodešle ani neuloží, jen vypíše |
| `python main.py --no-telegram` | uloží deník, zprávu vypíše do konzole |
| `python main.py --status` | přehled deníku a úspěšnosti |
| `python main.py --results-only` | jen dohledá výsledky |
| `python main.py -v` | podrobný log s důvody vyřazení |

### API klíč a Telegram

1. **The Odds API**: <https://the-odds-api.com/#get-access> — klíč přijde
   e-mailem zdarma, vlož ho do `.env` jako `ODDS_API_KEY`.
2. **Telegram**: v **@BotFather** dej `/newbot`, token vlož do `.env`.
   Pak **napiš svému botovi** `/start` a spusť `python telegram_setup.py` —
   chat ID si najde a zapíše sám.

Když Telegram vrátí `401 Unauthorized`, token neplatí (typicky byl
přegenerován v BotFather). Aktuální najdeš v `/mybots → bot → API Token`.

---

## Automatické spouštění

### Hlavní způsob: GitHub Actions (bez zapnutého počítače, zdarma)

Agent běží na serverech GitHubu podle `.github/workflows/denni-tipy.yml`.
Počítač nemusí být zapnutý.

- **Kdy:** každý den krátce po 8:00 pražského času. GitHub plánované běhy
  **negarantuje** — při zátěži je zpožďuje o desítky minut a občas některý
  zahodí. Proto se pokus opakuje každou půlhodinu mezi 6:17 a 9:47 UTC; stačí,
  aby vyšel jediný. Agent sám hlídá, aby zpráva přišla **jednou denně a ne
  před 8:00** (GitHub neumí letní čas). Přeskočené běhy skončí dřív, než sáhnou
  na API, takže nestojí žádné kredity.
- **Když zpráva nepřijde ani do 10:00**, spusť si běh ručně: *Actions* →
  *Denni tipy* → *Run workflow*. Zabere to pár vteřin.
- **Cena:** zdarma. Soukromý repozitář má 2000 minut měsíčně, agent spotřebuje
  zhruba 240 minut (8 pokusů denně po necelé minutě).
- **Klíče** jsou v GitHubu jako šifrované *secrets* (Settings → Secrets and
  variables → Actions). V repozitáři nejsou.
- **Deník tipů** (`tips.json`) si workflow po každém běhu zapíše zpět do
  repozitáře — v seznamu commitů uvidíš denně „Denik tipu 2026-09-16".
- **Změna nastavení:** na GitHubu otevři `settings.env`, klikni na tužku, uprav
  (například `ODDS_BANDS`) a dole *Commit changes*. Platí od dalšího běhu.
- **Ruční spuštění:** záložka *Actions* → *Denni tipy* → *Run workflow*.
  Pro test zaškrtni „Poslat i když už dnes zpráva byla".
- **Když něco selže**, GitHub pošle e-mail. Podrobný výpis je u běhu v *Actions*.

Lokální statistiky po přechodu: deník je na GitHubu, takže před
`python main.py --status` si ho stáhni příkazem `git pull`.

### Záložní způsob: Plánovač úloh ve Windows

Používej jen jedno z obou — jinak by chodily dvě zprávy a deníky by se
rozcházely. Po přechodu na GitHub je lokální úloha vypnutá:

```powershell
Disable-ScheduledTask -TaskName "BetAgent"   # vypnout
Enable-ScheduledTask  -TaskName "BetAgent"   # znovu zapnout
```

Úloha běží denně v 8:00; když je počítač vypnutý, spustí se hned po zapnutí.

Znovu vytvořit:

```powershell
$dir = "C:\Users\petr.shromazdil\Desktop\bet-agent"
$py  = "C:\Users\petr.shromazdil\AppData\Local\Python\pythoncore-3.14-64\python.exe"
$act = New-ScheduledTaskAction -Execute $py -Argument "main.py" -WorkingDirectory $dir
$trg = New-ScheduledTaskTrigger -Daily -At 8:00
Register-ScheduledTask -TaskName "BetAgent" -Action $act -Trigger $trg -Force
```

Okno pokrývá 24 hodin dopředu, takže ranní běh zahrne i noční NBA zápasy,
které v SEČ začínají po půlnoci. Logy ze všech běhů jsou v `agent.log`.

Na Linuxu přes cron:
```cron
0 8 * * * cd /opt/bet-agent && /opt/bet-agent/.venv/bin/python main.py >> agent.log 2>&1
```

---

## Nastavení (`settings.env`)

Běžné nastavení je v `settings.env` — ten je v repozitáři a dá se upravit
přímo na GitHubu. Tajné klíče (`ODDS_API_KEY`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`) jsou jen v GitHub secrets a v lokálním `.env`, **nikdy**
v `settings.env`.

| Proměnná | Výchozí | Význam |
|---|---|---|
| `ODDS_BANDS` | `1.15-1.19,1.20-1.24,1.25+` | kurzová pásma; nesmí se překrývat, `1.25+` = bez horní hranice (jen poslední) |
| `TIPS_PER_BAND` | `3` | kolik tipů v každém pásmu |
| `VIRTUAL_STAKE` | `100` | sázka pro virtuální zisk ve statistikách |
| `MIN_TIP_PROB` | `0.0` | tvrdý práh šance; `0` = bez prahu |
| `SPORT_PROFILE` | `core` | `individual` \| `low_variance` \| `core` \| `wide` |
| `MAX_SPORT_KEYS` | `12` | strop počtu dotazovaných soutěží |
| `ODDS_REGIONS` | `eu` | víc regionů = víc kanceláří i kreditů |
| `MIN_BOOKMAKERS` | `4` | míň kanceláří = odhadu nevěřit |
| `MAX_PROB_DISPERSION` | `0.04` | jak moc se smějí kanceláře rozcházet |
| `TWO_WAY_ONLY` | `false` | `true` vyřadí sporty s remízou (i házenou) |
| `MIN_MINUTES_BEFORE_START` | `45` | kolik času musí do začátku zbývat |
| `MAX_HOURS_AHEAD` | `24` | den i následující noc |
| `TRACK_RESULTS` | `true` | hlásit, jak dopadly minulé tipy |
| `ENABLE_SOCCER` | `false` | zapne drahou fotbalovou větev |

Jiná pásma? Například `ODDS_BANDS=1.12-1.20,1.21-1.30,1.31+`. Pásma dostanou
postupně zelenou, oranžovou, modrou a fialovou ikonu. Víc tipů? Zvyš `TIPS_PER_BAND`.

Na Windows vyžaduje `zoneinfo` balíček `tzdata` — je v `requirements.txt`.

---

## Řešení problémů

| Projev | Příčina a řešení |
|---|---|
| `chybi ODDS_API_KEY` | běžíš z jiného adresáře — spouštěj s `cd` do projektu |
| `HTTP 401` (Odds API) | neplatný klíč The Odds API |
| Telegram `401 Unauthorized` | token byl přegenerován, vezmi nový v BotFather |
| Telegram `403 Forbidden` | nenapsal jsi botovi jako první, nebo špatné chat ID |
| `HTTP 429` | vyčerpaná kvóta — sniž `MAX_SPORT_KEYS` |
| Nízké šance u všech tipů | sezónnost, ne chyba — v září je nabídka nejslabší |
| Chybí noční zápasy | zkontroluj `SAME_DAY_ONLY=false` |
| `UnicodeEncodeError` | starší konzole; agent si přepíná stdout na UTF-8, jinak `chcp 65001` |

Diagnostika: `python main.py --dry-run -v` vypíše u každého zápasu, proč
vypadl (chybí trh / málo kanceláří / neshodnou se / pozdě).

---

## Omezení

- **Kurzy se hýbou.** Kurz ve zprávě platil při stažení; než sázku podáš,
  může být jinde. Šance se tím ale prakticky nemění.
- **Agent čte jen kurzy.** Žádná forma, zranění ani sestavy. Trh tyhle věci
  většinou zaceňuje, ale ne vždy.
- **Konsensus není pravda.** Když se mýlí celý trh, mýlí se i agent.
- **Výsledky** se dohledávají do 3 dnů zpětně; starší tipy se označí jako
  nevyhodnocené.
- **Agent nesází.** Nepřipojuje se k žádnému sázkovému účtu.

---

## Licence a odpovědnost

Kód je určen pro osobní a vzdělávací použití. Nejde o investiční ani sázkové
poradenství. Sázení je hazardní hra se záporným očekáváním pro většinu hráčů —
autor ani nástroj neručí za tvoje výsledky.
