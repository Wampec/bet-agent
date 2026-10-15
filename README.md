# Bet Agent — denní tipy na Telegram

Python agent, který jednou denně projde nabídku tenisu, basketbalu a házené,
spočítá **skutečnou šanci favorita na výhru**, rozdělí zápasy do tří
kurzových pásem a z každého pošle na Telegram tři nejjistější zápasy dne i noci.

> **Stav od 15. 10. 2026:** denní rozesílání tipů je **vypnuté** (workflow
> *Denni tipy* na GitHubu je zakázaný). Běží jen **hlídač živých kurzů**
> na počítači — viz kapitola [Hlídač živých kurzů](#hlídač-živých-kurzů-hlidacpy).
> Denní agent se zapne zpět v *Actions → Denni tipy → Enable workflow*.

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
| 🟢 **A** | 1,03–1,12 | 7–24 výher v řadě — tohle pásmo nehoní 2×, honí jistotu |

**Režim „tutovky".** Rozbor deníku ze září–října 2026 (177 vyhodnocených
tipů) ukázal, že kurzy jsou přesně kalibrované: v pásmu 1,20–1,40 prohrává
každý čtvrtý tip, ať se vybírá jakkoli, a žádný filtr nad kurzy to nezmění.
Vysokou úspěšnost mají jen opravdu krátké kurzy: do 1,15 vyšlo 36/39,
a všechny tři prohry měly společný znak — dva tenis (WTA), jedna týmový favorit
venku. Týmoví favorité doma: 12/12. Z toho plyne nastavení:

- kurz 1,03–1,12 (`ODDS_BANDS`), při malé nabídce až 1,15 (`WIDE_ODDS_BANDS`),
- jen týmoví favorité doma (`FAVOURITE_HOME_ONLY=true`), fotbal jako „domácí
  favorit neprohraje" (1X),
- bez ženského tenisu, přípravy, letní ligy a exhibic (`SPORT_KEY_BLACKLIST`),
- mužský tenis jen jako záloha, když je tipů málo (`FALLBACK_SPORT_GROUPS`).

Cena za jistotu: zdvojnásobení vkladu sérií sázek při kurzu 1,08 vyžaduje
9 výher v řadě a vyjde zhruba ve 36 % případů; při kurzu 1,28 jsou to 3 výhry
a 43 %. Série „jistých" sázek tedy není bezpečnější cesta k 2× než jedna
sázka — jen platí marži vícekrát. Tenhle režim dává smysl, když je cílem co
nejvyšší podíl správných tipů, ne rychlé zdvojnásobení.

Pásem může být i víc (`ODDS_BANDS=1.15-1.19,1.20-1.24,1.25-1.29`), zpráva je
pak rozdělí a statistiky vede zvlášť.

**Když je tipů málo, pásmo se pro ten den rozšíří** na `WIDE_ODDS_BANDS`
(1,03–1,15). Spustí se to, až když v původním pásmu není aspoň
`FALLBACK_MIN_TIPS` (5) příležitostí — a to i po přidání záložních sportů.
Zpráva pak ukáže širší rozsah a připíše, že pásmo je dnes širší.

- **Zařazení podle běžného kurzu** — tedy mediánu napříč kancelářemi, zaokrouhleného
  na setiny. Nejlepší kurz často nabízí burza (Betfair, Matchbook) a české kanceláře
  v datech nejsou, takže u své kanceláře dostaneš spíš kurz kolem mediánu.
  Nejlepší kurz je ve zprávě v závorce jako `max`.
- **V každém pásmu deset nejvyšších šancí** (`TIPS_PER_BAND`). Protože vyšší
  šance = nižší kurz, tipy se drží spíš u spodní hranice pásma.
- **Krátké kurzy znamenají málo tipů.** Týmových favoritů doma s kurzem do
  1,15 vidí deník zhruba jednoho za dva dny; s fotbalem (1X ve 48 ligách)
  a od října s NBA jich bude víc, ale deset denně čekej jen o víkendech.
  V chudé dny se pásmo samo rozšíří na 1,03–1,15 (viz výše).
- **Tichý zbytek** — zápasy mimo pásmo se neposílají, ale ukládají se se
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
   (< 4) nebo se moc rozcházejí (> 4 p.b.). Čas do začátku se nehlídá — tipy
   chodí, dokud zápas nezačal (`MIN_MINUTES_BEFORE_START=0`).
4. **Pásma a seřazení** — zápas se zařadí podle běžného kurzu a v každém pásmu
   se pošlou tři s nejvyšší šancí.
5. **AI debata** (volitelná, výchozí vypnutá, viz níže): z kandidátů v pásmu vyberou
   GPT a Claude po rešerši na webu a vzájemné debatě tři „nejtutovější“.

### Jednu věc je fér vědět

Vysoká úspěšnost tipů **neznamená automaticky zisk**. V každém kurzu je marže
sázkovky, takže samotné sázení na favority má dlouhodobě zápornou očekávanou
hodnotu — i když vyhrají 19 z 20 zápasů. Agent hledá jistotu, ne výhodnost.
Kdyby ti šlo o dlouhodobý zisk, potřeboval bys hledat kurzy vyšší, než
odpovídá skutečné šanci; to je jiná úloha než tahle.

Sázej jen peníze, které můžeš ztratit. Linka pomoci: **800 222 222**
(Národní linka pro odvykání, ČR).

---

## Další kandidáti a text pro chat (zdarma)

Pod třemi hlavními tipy pásma zpráva vypíše **další kandidáty** až do
`LIST_PER_BAND` (10) zápasů na pásmo, každý na jeden řádek. Do statistik
úspěšnosti se nepočítají (ukládají se potichu), takže čísla pásem zůstávají
srovnatelná s historií.

Hned za hlavní zprávou přijde **druhá zpráva s hotovým textem pro chat**: zadání
pro AI a všechny vypsané zápasy (sport, soutěž, čas, tip, doma/venku, šance
trhu, kurz). Zkopíruj ji celou do Gemini, Clauda nebo ChatGPT. AI v chatu
vyhledá na webu formu, zranění, vzájemné zápasy a další podklady, seřadí zápasy
v každém pásmu a vybere tři nejjistější. **Nic to nestojí.**

```
Jsi zkušený sportovní analytik. Dnes je 22.09.2026. …
Úkol: 1. Ke každému zápasu vyhledej na webu … 3. Z každého pásma vyber 3 nejjistější tipy …

PÁSMO C (kurz 1,25–1,29)
C1 | Maria Sakkari – Linda Fruhvirtova | tenis, WTA Singapore Open | 22.09. 13:40 | tip: výhra Maria Sakkari | trh 78 % | kurz 1,25
C2 | Chicago Sky – Toronto Tempo | basketbal, WNBA | 23.09. 02:00 | tip: výhra Chicago Sky, hraje doma | trh 74 % | kurz 1,29
```

Vypíná se `CHAT_PROMPT=false`.

---

## AI debata o pořadí tipů

**Výchozí stav: vypnuto** (`AI_RANKING=false`) — místo placené debaty se posílá
text pro chat (viz výše). Zapnutí stojí asi 8–30 USD měsíčně podle sezóny.

Volitelný krok mezi výpočtem šance a odesláním zprávy. Z každého pásma se vezme
**až 5 kandidátů** místo 3 a o jejich pořadí se dohadují dva různí AI agenti.
Do zprávy jdou **3 nejlepší z každého pásma**, formát zprávy se nemění. U
každého tipu přibude jen poznámka s odhady obou agentů a hlavním argumentem.

**Jak debata probíhá:**

1. **Rešerše.** GPT s vyhledáváním na webu připraví ke **každému zápasu
   zvlášť** rozbor jako před zápasem: formu s výsledky, zranění a sestavy,
   vzájemné zápasy, herní styl a jak sedí na soupeře, tabulku a motivaci,
   program a únavu, povrch nebo počasí, co čekají preview a tipéři, a nakonec
   hlavní důvody pro favorita a rizika překvapení. Co nenašel, výslovně uvede.
   Vyhledávání je **vynucené** (model nesmí psát z paměti, ta je zastaralá)
   a omezené na `OPENAI_MAX_SEARCHES` (6) hledání na zápas. Stejné podklady
   pak dostanou oba agenti.
2. **Úvodní verdikty.** Každý agent nezávisle odhadne u každého kandidáta šanci
   na výhru (0–100 %) a napíše argument. Každý má jinou roli:
   - **GPT = analytik.** Staví na datech: forma, vzájemné zápasy, doma
     a venku, program, tabulka. Velká jména na něj neplatí.
   - **Claude = skeptik.** Hledá, proč favorit prohraje: absence, rotace,
     únava, cestování, motivace, nevhodný herní styl, počasí, chybějící data.
3. **Debata.** Shoda znamená **stejné 1. místo a odhady jeho šance nejvýš
   `AI_AGREE_GAP` (10) bodů od sebe**. Posuzuje se proto i pásmo s jediným
   zápasem: když jeden agent dá favoritovi 85 % a druhý 65 %, musí se
   dohadovat. Kde shoda není, střídají se v odpovědích (GPT, Claude,
   GPT…). Každý vidí soupeřovy odhady a argumenty a musí na nejsilnější
   z nich odpovědět. Názor smí změnit jen kvůli konkrétnímu faktu, ne proto,
   aby debata skončila. Debata končí shodou, nejpozději po `AI_MAX_TURNS` (4)
   odpovědích.
4. **Verdikt spočítá kód, ne třetí AI.** Na 1. místo jde zápas, na kterém se
   shodli. Pokud se neshodli, rozhodne vyšší průměr obou odhadů a zpráva to
   označí. Zbytek pásma se seřadí podle průměru obou odhadů.

```
🟢 PÁSMO A · kurz 1,15–1,19
1. Carlos Alcaraz — 86,0 % · kurz 1,15
    🎾 ATP Peking · dnes 11:30 · proti Kvalifikant
    🤝 GPT 90 % · Claude 87 %: Soupeř hrál včera tři hodiny kvalifikaci, Alcaraz odpočatý.
2. Boston Celtics — 81,1 % · kurz 1,21
    🏀 NBA · zítra 01:30 · proti Washington Wizards
    ❔ GPT 82 % · Claude 78 %: O sestavě Celtics rešerše nic nenašla.

Pořadí určila debata AI: GPT (s rešerší na webu) x Claude, čísla = jejich
odhad šance. 🤝 = shoda, ⚠️ = neshodli se, rozhodl průměr, ❔ = málo dat.
```

**Celý průběh debaty** (rešerše se zdroji, všechny odhady a odpovědi) se každý
den uloží do složky [`debaty/`](debaty/) jako `RRRR-MM-DD.md`, a to i na GitHubu.

**Když něco selže:** pokud rešerše jednoho pásma selže, debata pokračuje bez
dat z webu a agenti to vědí. Pokud selže některý agent (výpadek, neplatná
odpověď), zpráva přijde s pořadím podle kurzů a řádkem „AI debata dnes
selhala“. AI nikdy nezpůsobí, že zpráva nepřijde.

### Zapnutí: dva klíče

Bez **obou** klíčů je krok neaktivní a tipy jdou podle kurzů jako dřív.

**Anthropic (Claude):**
1. Na <https://console.anthropic.com> si založ účet, v *Billing* dobij kredit
   a v *Settings → Limits* nastav **měsíční limit útraty**, např. 5 USD.
2. *Settings → API keys → Create key*. Klíč se zobrazí jen jednou.
3. Na GitHubu: *Settings → Secrets and variables → Actions → New repository
   secret* → název **`ANTHROPIC_API_KEY`**.

**OpenAI (GPT):**
1. Na <https://platform.openai.com> si založ účet, v *Settings → Billing*
   dobij kredit (stačí 10 USD) a v *Settings → Limits* nastav měsíční limit.
2. *API keys → Create new secret key*. Klíč se zobrazí jen jednou.
3. Na GitHubu stejně jako výše, název **`OPENAI_API_KEY`**.

Oba klíče můžeš volitelně dát i do lokálního `.env`.

### Cena

- **GPT** (`gpt-5-mini`: 0,25 / 2 USD za milion tokenů, hledání 0,01 USD):
  rešerše ke každému zápasu (typicky 5–15 denně, nejvýš 6 hledání na zápas),
  analytik 1–3 odpovědi. **Orientačně 6–10 USD měsíčně**, v plné sezóně
  s více zápasy až ~15. Většinu ceny tvoří hledání.
- **Claude** (`claude-sonnet-5`, 2 / 10 USD za milion tokenů): úvodní verdikt
  a nejvýš 2 odpovědi denně. Úvodní zadání s rešerší jde do prompt cache, takže
  opakované čtení stojí desetinu. **Orientačně 2–4 USD měsíčně.** Přesná
  spotřeba obou je v logu (`AI debata hotova: Claude … GPT … celkem ~0.30 USD`).

### Pomáhá to vůbec? Měří se to

AI je hypotéza, ne jistota. Do deníku se proto ukládá **AI pořadí i pořadí podle
kurzů**. Tipy, které by podle kurzů šly do zprávy, ale AI je vyřadila, se uloží
potichu (neposílají se). Po několika týdnech `python main.py --status` ukáže:

```
AI vs. čistý kurzový výběr (top 3 v pásmu, jen dny, kdy AI běžela):
  výběr AI:                    41/50 (82%)
  výběr podle kurzu:           40/50 (80%)
  jen AI (kurzy ne):           5/7 (71%)
  jen kurzy (AI je vyřadila):  4/7 (57%)
  Rozhodují dva poslední řádky -- tam se AI a kurzy liší.
```

Rozhodují **dva poslední řádky**, protože jen tam se AI a kurzy skutečně liší.
Malé vzorky nic nedokazují: pár desítek tipů rozdíl 2 % nepotvrdí ani nevyvrátí.
Pokud AI po měsících nevychází lépe, vypni ji (`AI_RANKING=false`). Agent se
vrátí k pořadí podle kurzů a přestaneš za AI platit.

### Nastavení

| Proměnná | Výchozí | Význam |
|---|---|---|
| `AI_RANKING` | `false` | zapnutí kroku (běží jen s oběma klíči) |
| `AI_POOL_PER_BAND` | `5` | kolik kandidátů z pásma jde do debaty |
| `AI_MAX_TURNS` | `4` | nejvýš odpovědí v debatě po úvodních verdiktech |
| `AI_AGREE_GAP` | `10` | shoda = stejné 1. místo a odhady nejvýš N bodů od sebe |
| `AI_MODEL` | `claude-sonnet-5` | model skeptika |
| `AI_EFFORT` | `medium` | `low` / `medium` / `high`, hloubka uvažování obou agentů |
| `OPENAI_RESEARCH_MODEL` | `gpt-5-mini` | model pro rešerši s vyhledáváním na webu |
| `OPENAI_MODEL` | `gpt-5-mini` | model analytika v debatě |
| `OPENAI_MAX_SEARCHES` | `6` | nejvýš hledání na webu na jeden zápas |
| `OPENAI_RESEARCH_EFFORT` | `medium` | `low` / `medium` / `high`, jak důkladně GPT rešerši promýšlí |


---

## Proč zrovna tyhle sporty

| Sport | Trh | Poznámka |
|---|---|---|
| **Tenis (jen ATP)** | dvoucestný | Žádná remíza. V raných kolech turnajů bývají favorité přes 90 %. Při skreči většina kanceláří sázku ruší. **Ženský tenis je vyřazený** (`SPORT_KEY_BLACKLIST=tennis_wta_*`) — je výrazně variabilnější než mužský. |
| **Basketbal** | dvoucestný | Z týmových sportů nejmíň náhodný — hodně skórování znamená, že lepší tým vyhrává častěji. |
| **Házená** | **trojcestný** | Remíza je možná (bývá 3–8 %) a je to **prohraná sázka**. Spočtená šance to už zohledňuje, tip je ve zprávě označený. |
| **Americký fotbal** | dvoucestný | Hodně výrazných favoritů, hlavně v univerzitní lize (NCAAF): v týdenním testu padlo do pásem 19 zápasů se šancí 80–85 %. Hraje se ale skoro jen v sobotu (NFL v neděli a v noci na pondělí), takže ve zbytku týdne nenabídne nic. Sezóna září–leden. |
| Baseball, hokej | dvoucestný | Jen v profilu `wide`. Výrazné favority mají vzácněji — v testu nepadl do pásem ani jeden zápas. |
| **Fotbal** | **1X (dvojitá šance)** | **Všechny aktivní ligy** (`SOCCER_LEAGUE_WHITELIST=ALL`, aktuálně 48 — od Ligy mistrů po Brazílii, Japonsko nebo Finsko), takže fotbal hraje každý den. Výhra favorita se do pásma vejde zřídka, tip „favorit vyhraje nebo remizuje" podstatně častěji. |

Profily: `individual` (jen tenis), `low_variance` (tenis + basketbal),
`core` (tenis + basketbal + házená),
**`core_us` (výchozí: + americký fotbal)**, `wide` (+ baseball a hokej).
Vlastní sestavu nastavíš přes `SPORT_GROUPS` (má přednost před profilem).

### Záložní sporty

`FALLBACK_SPORT_GROUPS=Tennis` znamená, že tenis **není** v běžném dotazu. Agent
nejdřív projde ostatní sporty (basketbal, házená, americký fotbal, fotbal) a teprve
když v pásmech nenajde aspoň `FALLBACK_MIN_TIPS` (10) příležitostí, dotáhne i tenis.
Ve dnech s dostatkem jiných zápasů tak tenis nestojí ani kredit a nabídka zůstane
u sportů, kterým věříš víc. V logu je vidět, co se stalo:

```
V pasmech je jen 1 prilezitosti (chci 10) -- pridavam zalozni sporty: Tennis.
Prilezitosti v pasmech: 12 -- zalozni sporty (Tennis) dnes netreba.
```

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
| Program fotbalových soutěží | **0** — endpoint `/events` je zdarma |
| Fotbalové kurzy | 1 za ligu, která dnes hraje, nejvýš `SOCCER_MAX_LEAGUES` (6) |
| Fotbalové trhy 1X | 1 za zápas, nejvýš `MAX_EVENT_MARKET_LOOKUPS` (3) |
| Záložní sporty (tenis) | 0 ve dnech, kdy hlavní sporty dají dost tipů — vůbec se nedotazují |
| **Reálně** | **~5–20 za den** |

Soutěž bez zápasů v časovém okně **nestojí nic** — devět dotázaných soutěží
v testu sebralo jen 4 kredity. U fotbalu jde agent ještě dál: program všech
48 lig si projde zdarma (`/events`) a kurzy koupí jen u těch, které dnes
opravdu hrají, nejvýš u `SOCCER_MAX_LEAGUES`. V měření 8. 10. 2026 hrálo
7 lig ze 48, takže sken nestál nic a zaplatilo se za 6.
Předvýběr fotbalu navíc míří do pásem: u největších favoritů je dvojitá šance
kolem 1,02, což by byl vyhozený kredit, takže se dotahují zápasy s 1X zhruba
mezi 1,15 a 1,29.

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
├── ai_debate.py volitelná AI debata o pořadí: rešerše + GPT x Claude
├── ai_ranker.py kandidáti pro AI a použití výsledného pořadí
├── gpt.py      klient OpenAI Responses API (vyhledávání na webu)
├── notifier.py sestavení a odeslání zprávy (MarkdownV2)
└── state.py    deník tipů a statistiky pásem

hlidac.py       hlídač živých kurzů (běží na tvém počítači, viz kapitola níže)
hlidac_pripady.csv  zapsané případy hlídače
hlidac.log          výpis hlídače (není v repozitáři)

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

- **Kdy:** přesně v 8:00. Spouští to externí služba **cron-job.org**, která
  v 8:00 pražského času zavolá GitHub API a běh začne během pár vteřin
  (nastavení níže). Vlastní plánovač GitHubu se na čas použít nedá — v tomhle
  repozitáři zpožďoval běhy o 6–7 hodin — a zůstává jen jako pozdní záloha.
  Agent sám hlídá, aby zpráva přišla **jednou denně**; přeskočené běhy skončí
  dřív, než sáhnou na API, a nestojí žádné kredity.
- **Když zpráva nepřijde**, spusť si běh ručně: *Actions* → *Denni tipy* →
  *Run workflow*. Zabere to pár vteřin.
- **Cena:** zdarma. GitHub (2000 minut měsíčně pro soukromý repozitář, agent
  spotřebuje ~60) i cron-job.org.
- **Klíče** jsou v GitHubu jako šifrované *secrets* (Settings → Secrets and
  variables → Actions). V repozitáři nejsou.
- **Deník tipů** (`tips.json`) si workflow po každém běhu zapíše zpět do
  repozitáře — v seznamu commitů uvidíš denně „Denik tipu 2026-09-16".
- **Změna nastavení:** na GitHubu otevři `settings.env`, klikni na tužku, uprav
  (například `ODDS_BANDS`) a dole *Commit changes*. Platí od dalšího běhu.
- **Ruční spuštění:** záložka *Actions* → *Denni tipy* → *Run workflow*.
  Pro test zaškrtni „Poslat i když už dnes zpráva byla". Volba „Jen zkouška“
  projde celý běh včetně AI debaty, ale nic nepošle ani neuloží — výsledek
  a průběh debaty jsou jen ve výpisu běhu.
- **Když něco selže**, GitHub pošle e-mail. Podrobný výpis je u běhu v *Actions*.

Lokální statistiky po přechodu: deník je na GitHubu, takže před
`python main.py --status` si ho stáhni příkazem `git pull`.

#### Spouštění v 8:00 přes cron-job.org

**1. Token, který smí jen spustit tenhle workflow** — na GitHubu vpravo nahoře
profilová fotka → *Settings* → úplně dole *Developer settings* → *Personal access
tokens* → *Fine-grained tokens* → *Generate new token*:

| Pole | Hodnota |
|---|---|
| Token name | `cron-job bet-agent` |
| Expiration | nejdelší nabízená doba |
| Repository access | *Only select repositories* → `bet-agent` |
| Permissions → Repository permissions → **Actions** | **Read and write** |

*Generate token* a token si hned zkopíruj — znovu už zobrazit nejde.
Nic jiného token nesmí, takže kdyby unikl, dá se jím jen spustit tenhle běh.

**2. Úloha na cron-job.org** — založ si účet na <https://cron-job.org> →
*Cronjobs* → *Create cronjob*:

| Pole | Hodnota |
|---|---|
| Title | `Bet Agent` |
| URL | `https://api.github.com/repos/Wampec/bet-agent/actions/workflows/denni-tipy.yml/dispatches` |
| Execution schedule | každý den (*Every day*) v **8:00** |
| *Advanced* → Time zone | **Europe/Prague** |
| *Advanced* → Request method | **POST** |
| *Advanced* → Request body | `{"ref":"main"}` |
| *Advanced* → Headers | `Authorization` = `Bearer TVUJ_TOKEN` |
| | `Accept` = `application/vnd.github+json` |
| | `X-GitHub-Api-Version` = `2022-11-28` |

Ulož a vyzkoušej tlačítkem *Test run*. Správná odpověď je **204** (bez obsahu)
a v GitHubu v *Actions* se hned objeví nový běh. Pokud už dnes zpráva přišla,
běh proběhne, ale podruhé ji nepošle — to je správně.

Když token vyprší, cron-job.org začne hlásit chybu **401** (a pošle e-mail) —
stačí vygenerovat nový a vyměnit ho v hlavičce `Authorization`.

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

## Hlídač živých kurzů (`hlidac.py`)

Druhý, nezávislý program. Nehledá tipy před zápasem, ale **jednu konkrétní
situaci v zápase**, u které historická data ukazují, že trh favorita
podceňuje:

> Fotbalový favorit s kurzem do 1,5 dostane gól do 15. minuty a prohrává 0:1.

Podle dat z 16 evropských lig (2000–2023, ~900 takových zápasů) favorit přesto
vyhraje v ~54 % a neprohraje v ~75 % — výrazně víc, než by odpovídalo prostému
modelu (41 % / 67 %). Čím silnější favorit, tím větší rozdíl: u kurzu kolem
1,20 je to 62 % proti 48 %. Férové kurzy z toho vycházejí takhle:

| Kurz favorita před zápasem | Férový kurz na výhru | Férový kurz na dvojtip (1X / X2) |
|---|---|---|
| do 1,40 | 1,61 | 1,23 |
| 1,40–1,50 | 2,05 | 1,41 |

**Hranice VALUE** = férový kurz + 5 % (`HLIDAC_VALUE_MARGIN`): výhra nad 1,69
resp. 2,15, dvojtip nad 1,29 resp. 1,48. Pokud kancelář po gólu nabídne víc,
sázka má kladnou hodnotu. Pokud skočí jen na 1,8, kancelář efekt zná a výhoda
není. **Právě to zbývá ověřit** — proto hlídač v prvních týdnech jen zapisuje.

Důležité: férové kurzy pocházejí z dat bez kurzů (síla favorita odhadnutá
z Elo), takže jsou to hypotézy. Zapsané případy je buď potvrdí, nebo vyvrátí.

### Odkud bere data (zdarma, bez registrace)

| Co | Zdroj | Cena |
|---|---|---|
| Program, favorit a předzápasový kurz | ESPN (neoficiální veřejné API, kurzy DraftKings) | zdarma, bez klíče |
| Živé skóre a minuta gólu | ESPN | zdarma, bez klíče |
| Živý kurz po gólu (medián evropských kanceláří) | The Odds API (`ODDS_API_KEY`) | 1 kredit, jen při spuštění pravidla |
| Konečný výsledek | ESPN | zdarma |

Pravidlo se spouští zhruba 2–4× za víkend, takže hlídač spotřebuje asi
10–20 kreditů The Odds API měsíčně (strop `HLIDAC_MAX_ODDS_CALLS` denně).
Sleduje 19 soutěží: anglickou 1. a 2. ligu, Španělsko, Německo, Itálii,
Francii, Nizozemsko, Portugalsko, Belgii, Turecko, Skotsko, Rakousko,
Švýcarsko, Řecko, Norsko, Dánsko a tři evropské poháry. **Česká liga v ESPN
není.** Dvojtip se z živé nabídky nestahuje zvlášť (stál by kredit navíc),
hlídač ho odhaduje z kurzu na výhru a remízu.

Rizika: ESPN API je neoficiální a může se bez varování změnit (`python hlidac.py
diag` to odhalí). API-Football by bylo oficiální, ale bezplatný tarif má jen
100 dotazů denně a podle všeho jen starší sezóny, takže by vyšlo na Pro za
19 USD měsíčně.

### Co hlídač dělá

1. Ráno (`HLIDAC_PREP_HOUR`) stáhne program sledovaných soutěží a pošle na
   Telegram seznam dnešních favoritů do 1,5.
2. Od výkopu do `HLIDAC_WINDOW_MINUTES` (22. minuty) kontroluje skóre každých
   `HLIDAC_INTERVAL_SECONDS` (60 s).
3. Když padne 0:1 proti favoritovi do 15. minuty, stáhne živé kurzy, porovná je
   s férovou hranicí a pošle zprávu **VALUE** / **bez value**. Případ zapíše do
   `hlidac_pripady.csv`.
4. Po zápase dopíše výsledek. `python hlidac.py report` spočítá, jak často byl
   kurz nad hranicí a jaký by byl zisk.

**Hlídač nic nesází.** Když přijde VALUE, rozhoduje, co v tu chvíli nabízí
Tipsport nebo Fortuna.

### Spouštění na počítači

Hlídač se spouští sám přes Plánovač úloh Windows (úloha **Hlidac zivych
kurzu**): při přihlášení a každý den v 7:55, běží skrytě a druhou instanci
nespustí. Výpis je v `hlidac.log`. Počítač musí být zapnutý a nesmí usnout
(*Nastavení → Napájení → Režim spánku: Nikdy*).

| Příkaz | Co dělá |
|---|---|
| `python hlidac.py plan 2026-10-17` | favorité daného dne (zdarma, nic neposílá) |
| `python hlidac.py diag` | kontrola soutěží, kreditů a Telegramu |
| `python hlidac.py run` | hlídání ručně v okně (Ctrl+C ukončí) |
| `python hlidac.py report` | vyhodnocení zapsaných případů |

Vypnutí: *Plánovač úloh → Hlidac zivych kurzu → Zakázat*, nebo v PowerShellu
`Disable-ScheduledTask "Hlidac zivych kurzu"`.

Takových zápasů je v Evropě zhruba 2–4 za víkend, takže za měsíc nebo dva bude
jasné, jestli kanceláře nad férovou hranici chodí.

---

## Nastavení (`settings.env`)

Běžné nastavení je v `settings.env` — ten je v repozitáři a dá se upravit
přímo na GitHubu. Tajné klíče (`ODDS_API_KEY`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`) jsou jen v GitHub secrets a v lokálním `.env`, **nikdy**
v `settings.env`.

| Proměnná | Výchozí | Význam |
|---|---|---|
| `ODDS_BANDS` | `1.03-1.12` | kurzová pásma; nesmí se překrývat. Zápis `1.25+` by značil pásmo bez horní hranice (jen poslední) |
| `TIPS_PER_BAND` | `10` | kolik hlavních tipů v každém pásmu (počítají se do statistik) |
| `LIST_PER_BAND` | `10` | kolik zápasů na pásmo vypsat celkem (hlavní tipy + další kandidáti) |
| `WIDE_ODDS_BANDS` | `1.03-1.15` | širší pásmo pro dny s malou nabídkou; prázdné = nerozšiřovat |
| `CHAT_PROMPT` | `true` | druhá zpráva s hotovým textem pro chat |
| `VIRTUAL_STAKE` | `100` | sázka pro virtuální zisk ve statistikách |
| `MIN_TIP_PROB` | `0.0` | tvrdý práh šance; `0` = bez prahu |
| `FAVOURITE_HOME_ONLY` | `true` | jen týmoví favorité doma (tenis se netýká) |
| `SPORT_PROFILE` | `core_us` | `individual` \| `low_variance` \| `core` \| `core_us` \| `wide` |
| `SPORT_GROUPS` | (prázdné) | vlastní seznam skupin; má přednost před profilem |
| `SPORT_KEY_BLACKLIST` | `tennis_wta_*,*preseason*,*summer_league*,*all_stars*` | vyřazené soutěže; smí být vzor s hvězdičkou |
| `MAX_SPORT_KEYS` | `12` | strop počtu dotazovaných soutěží |
| `ODDS_REGIONS` | `eu` | víc regionů = víc kanceláří i kreditů |
| `MIN_BOOKMAKERS` | `4` | míň kanceláří = odhadu nevěřit |
| `MAX_PROB_DISPERSION` | `0.04` | jak moc se smějí kanceláře rozcházet |
| `TWO_WAY_ONLY` | `false` | `true` vyřadí sporty s remízou (i házenou) |
| `MIN_MINUTES_BEFORE_START` | `0` | odstup před začátkem zápasu; `0` = tipy chodí, dokud zápas nezačal |
| `MAX_HOURS_AHEAD` | `24` | den i následující noc |
| `TRACK_RESULTS` | `true` | hlásit, jak dopadly minulé tipy |
| `HLIDAC_LEAGUES` | 19 soutěží | kódy soutěží ESPN, které hlídač sleduje |
| `HLIDAC_MAX_ODDS` | `1.5` | hlídají se jen favorité s kurzem do této hodnoty |
| `HLIDAC_GOAL_MINUTE` | `15` | gól soupeře musí padnout do této minuty |
| `HLIDAC_INTERVAL_SECONDS` | `60` | jak často se kontroluje skóre (ESPN je zdarma) |
| `HLIDAC_MAX_ODDS_CALLS` | `10` | denní strop dotazů na živé kurzy (1 kredit = 1 dotaz) |
| `HLIDAC_VALUE_MARGIN` | `1.05` | VALUE = živý kurz aspoň o 5 % nad férovým |
| `ENABLE_SOCCER` | `true` | zapne fotbalovou větev (trhy po zápasech) |
| `SOCCER_WEEKDAYS` | (prázdné) | dny, kdy se fotbal tahá (1 = pondělí); prázdné = každý den |
| `FALLBACK_SPORT_GROUPS` | `Tennis` | skupiny, které se dotáhnou jen při nedostatku tipů |
| `FALLBACK_MIN_TIPS` | `5` | kolik tipů v pásmech stačí, aby se záloha nebrala |
| `SOCCER_MARKETS` | `double_chance` | `double_chance` (1X) a/nebo `draw_no_bet`; každý trh = 1 kredit za zápas |
| `SOCCER_LEAGUE_WHITELIST` | `ALL` | `ALL` = všechny ligy, prázdné = šest největších, nebo vlastní klíče |
| `SOCCER_MAX_LEAGUES` | `6` | kolik hrajících lig denně smí stát kredity (přednost mají poháry a top ligy) |

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
