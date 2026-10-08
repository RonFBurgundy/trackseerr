# Quality system research: community-backed defaults

Researched 2026-10-05. Tags: **[S]** sourced verbatim from a URL, **[D]** derived by us (arithmetic or inference), **[O]** our suggestion/opinion. Anything not verified is under "Not verified".

Headline findings that change the plan:

1. **TRaSH Guides has no Lidarr content.** Their Lidarr page says so and points to the "Davo community guide" ([index.md](https://raw.githubusercontent.com/TRaSH-Guides/Guides/master/docs/Lidarr/index.md)). No `docs/json/lidarr` directory exists in the TRaSH repo (GitHub contents API for `docs/json` lists only `guide-only` and the arr dirs; `docs/json/lidarr` returned 404). The community guide has since been folded into the Servarr wiki ([community-guide.md](https://raw.githubusercontent.com/Servarr/Wiki/master/lidarr/community-guide.md) is a redirect stub). The real "TRaSH-equivalent" source is [Servarr wiki Lidarr Tips and Tricks, Custom Formats](https://raw.githubusercontent.com/Servarr/Wiki/master/lidarr/tips-and-tricks.md).
2. **Lidarr has no "Source" custom-format condition**, despite the wiki prose listing one. The code only has Release Title, Release Group, Indexer Flag, Size (see section 1.3). Source (CD/WEB/Vinyl) is title-regex only.
3. Lidarr's quality definitions are **kbps averaged over album duration**, and a **missing album duration rejects the release** when a bound is set.

---

## A. Summary of recommended defaults

### A.1 Quality definitions (kbps, size = kbps x duration)

Lidarr's own values are given for comparison. Min/Pref/Max are the proposal.

| Quality | Lidarr default min/pref/max | Proposed min | Proposed pref | Proposed max | Basis |
|---|---|---|---|---|---|
| FLAC 24bit | 0 / 895 / unlimited [S] | 0 | 2000 | 9500 | pref [D], max [D]; see note 1 |
| FLAC 16bit | 0 / 895 / unlimited [S] | 0 | 895 | 1400 | pref [S] Lidarr; max [S] Servarr tips (1400) |
| MP3 320 | 0 / 195 / 350 [S] | 290 | 320 | 350 | max [S]; pref [D] CBR; min [D] slack |
| MP3 V0 | 0 / 195 / 350 [S] | 160 | 245 | 350 | max [S]; pref [S] hydrogenaudio ~245; min [D] |
| AAC 256 | 0 / 95 / 280 [S] | 200 | 256 | 280 | max [S]; pref [D]; min [D] |
| MP3 192 | 0 / 95 / 210 [S] | 150 | 192 | 210 | max [S]; pref [D] |
| MP3 V2 | 0 / 95 / 280 [S] | 130 | 190 | 280 | max [S]; pref [S] ~190; min [D] |
| Unknown | 0 / 195 / 350 [S] | 0 | 195 | 350 | [S] Lidarr |

Notes:
1. FLAC 24: Servarr's tip caps it at **1495** with pref 895 ([tips-and-tricks](https://raw.githubusercontent.com/Servarr/Wiki/master/lidarr/tips-and-tricks.md)). That cap exists to catch single-file CUE+FLAC rips and it **rejects any real 24/96 release** (~2500+ kbps, see B.3). Lidarr's UI slider hard-caps at 1500 ([Quality/Definition/QualityDefinition.js](https://raw.githubusercontent.com/Lidarr/Lidarr/develop/frontend/src/Settings/Quality/Definition/QualityDefinition.js), `MAX = 1500`; also [issue #1146](https://github.com/lidarr/Lidarr/issues/1146)). We are not bound by that UI. 9500 = just above raw 24/192 stereo PCM (9216 kbps) [D].
2. Lidarr's quality "pref" is only used to sort candidates in the same quality; it does not gate. [O] Treat our `pref` the same way.
3. Min of 0 for lossless matches every Lidarr default. A non-zero lossless min catches truncated/partial downloads but mono, solo piano and quiet classical can compress under 400 kbps [D]. Keep 0 unless a duration-known check is wanted.
4. Lossy mins [D] reject mislabeled lower-bitrate files (e.g. a 128k file tagged 320). Lidarr uses 0 everywhere for lossy, so tighter mins are our addition and are the riskiest numbers here. Make mins user-editable and ship them conservative.

### A.2 Quality profile defaults

| Profile | Order (low to high) | Cutoff | Upgrades | Min CF score | Upgrade-until score | Basis |
|---|---|---|---|---|---|---|
| Lossless | FLAC 16, FLAC 24 (grouped as "Lossless" in Lidarr) | FLAC 16 | on | 0 | 0 | Lidarr "Lossless" profile [S]: cutoff FLAC, allows FLAC, ALAC, FLAC 24, ALAC 24 |
| Standard (lossy) | MP3 192, MP3 V2, AAC 256, MP3 V0, MP3 320 (our order, see below) | MP3 192 | on | 0 | 0 | Lidarr "Standard" [S]: cutoff MP3-192, allows only 192, 256, 320; the V0/V2/AAC entries are [O] |
| Any | everything | Unknown | on | 0 | 0 | Lidarr "Any" [S] |
| Hi-Res first [O] | FLAC 16, FLAC 24 | FLAC 24 | on | 0 | 100 | [O]; mirrors Tubifarry's order FLAC 24 above FLAC ([Tubifarry #138](https://github.com/TypNull/Tubifarry/discussions/138)) |

Our lossy ordering [O], high to low: MP3 320, MP3 V0, AAC 256, MP3 V2, MP3 192. Lidarr's own weights (code, [Quality.cs](https://raw.githubusercontent.com/Lidarr/Lidarr/develop/src/NzbDrone.Core/Qualities/Quality.cs)) put MP3 192 (15) < V2 = 256 CBR = AAC 256 (18) < V0 (19) < 320 (20). Group V2/AAC 256/MP3 256 equivalents as one tier, as Lidarr does ("Mid Quality Lossy").

Lidarr's default profiles ship **Min Format Score 0 and Cutoff Format Score 0** [S] ([QualityProfileService.cs](https://raw.githubusercontent.com/Lidarr/Lidarr/develop/src/NzbDrone.Core/Profiles/Qualities/QualityProfileService.cs)). Servarr's example sets Min CF Score = **1** [S], which makes untagged releases un-grabbable. We should default to 0 [O].

### A.3 Default custom formats

Sourced scores come from two unaffiliated community lists. Where they disagree both are shown.

| Custom format | Condition | Servarr tips | Tubifarry | Proposed | Basis |
|---|---|---|---|---|---|
| Preferred Groups | ReleaseGroup `\b(DeVOiD\|PERFECT\|ENRiCH\|BigFLAC\|GalaxyLossless\|MusiCHI\|LoRD)\b` | 100 (3 groups) | +10 (7 groups) | 50 | groups [S]; score [O] |
| CD | Title `\bCD\b` | 10 | +2 | 10 | [S] |
| Lossless | Title `\b(FLAC\|Lossless\|ALAC\|APE\|WavPack)\b` | 10 | +1 | 5 | [S] regex; score [O] |
| WEB | Title `\bWEB\b` | 5 | +1 | 5 | [S] |
| Vinyl | Title `\bVinyl\b` | -10000 | -5 | -50 | [S] regex; score [O] (soft penalty) |
| Hi-Res 24bit | Title `24.?bit\|Hi.?Res` | n/a | +15 | 15 | [S] Tubifarry |
| Remastered | Title `[Rr]emaster(ed)?` | n/a | +10 | 0 | [S] regex; score [O], see C.3 |
| Deluxe Edition | Title `[Dd]eluxe` | n/a | +5 | 0 | [S] regex; score [O], see C.3 |
| Mono | Title `\bMono\b` | n/a | -10 | -10 | [S] |
| Censored/Clean | Title `\b(Censored\|Clean)\b` | n/a | -15 | -15 | [S] |
| Lossy-origin / fake lossless | see A.4 | n/a | n/a | -1000 | [O] |

### A.4 Default reject terms (release profile "Must Not Contain") [O unless noted]

No public source lists these for Lidarr. The only sourced anchors are that RED forbids transcodes ([RED rules](https://interviewfor.red/en/rules.html)) and that MQA is lossy ([Wikipedia](https://en.wikipedia.org/wiki/Master_Quality_Authenticated)). The term list itself is ours.

| Term (regex) | Why | Hard reject or -1000 CF |
|---|---|---|
| `transcode(d)?` | RED forbids transcode uploads [S]; self-labelled | reject |
| `up[-_ ]?(conver\w+\|sampl\w+)` | upconverted/upsampled hi-res | reject |
| `fake[-_ . ]?flac` | self-labelled | reject |
| `lossy[-_ ]?(master\|web\|flac)` | "lossy master" is RED vocabulary (autobrr thread, B.4) | reject |
| `\bMQA\b` | MQA is lossy [S Wikipedia]; "24bit" MQA files are folded 16-bit-ish PCM | reject |
| `\b(CUE\|single[-_ ]?file)\b` combined with FLAC | single-image rips are not importable per-track | CF -100, not reject |
| `\b(SBD\|AUD\|bootleg)\b` | optional, user preference | off by default |
| `\bwv?p\b`, `\.wvp` | Lidarr cannot import `.wvp` (vinyl-rip case, [haynesnetwork #610](https://github.com/thaynes43/haynesnetwork/issues/610)) | off by default |

Release profile semantics to copy (code-verified): required terms within one profile are OR; separate profiles AND; any ignored term rejects; terms wrapped `/pattern/flags` are .NET regex, else case-insensitive substring (see 1.4).

### A.5 Delay profile default [O, grounded in code]

Lidarr default: one "Default" untagged profile, protocols Usenet and Torrent both allowed, delay 0 each, first item = preferred protocol. Fields: per-protocol `Allowed` + `Delay` (minutes), `BypassIfHighestQuality`, `BypassIfAboveCustomFormatScore` + `MinimumCustomFormatScore`, tags, order. Ship delay 0 for everything; for Trackseerr's sources add "Soulseek/Deezer/other" as protocol items rather than a fixed pair.

### A.6 Metadata profile default

Lidarr "Standard": primary type Album only; secondary Studio only; release status Official only [S] ([MetadataProfileService.cs](https://raw.githubusercontent.com/Lidarr/Lidarr/develop/src/NzbDrone.Core/Profiles/Metadata/MetadataProfileService.cs)). An "empty/None" profile (everything off) also exists. We should copy Standard and offer a second "Albums + EPs + Singles" [O].

---

## B. Sources and evidence

### 1. Lidarr schemas and semantics

**1.1 Custom format JSON schema (importable).** From the Servarr wiki examples ([tips-and-tricks.md](https://raw.githubusercontent.com/Servarr/Wiki/master/lidarr/tips-and-tricks.md)) and the API resource ([CustomFormatResource.cs](https://raw.githubusercontent.com/Lidarr/Lidarr/develop/src/Lidarr.Api.V1/CustomFormats/CustomFormatResource.cs)):

```json
{
  "name": "Preferred Groups",
  "includeCustomFormatWhenRenaming": false,
  "specifications": [
    { "name": "DeVOiD", "implementation": "ReleaseGroupSpecification",
      "negate": false, "required": false, "fields": { "value": "\\bDeVOiD\\b" } }
  ]
}
```

- Top level: `name`, `includeCustomFormatWhenRenaming` (nullable bool, defaults false), `specifications[]`. API output adds `id`.
- `implementation` is the **C# class name** (resolved by `GetType().Name`): `ReleaseTitleSpecification`, `ReleaseGroupSpecification`, `IndexerFlagSpecification`, `SizeSpecification`. Unknown names throw.
- `fields` in the import/export JSON is an object `{ "value": ... }`. The REST API serialises it as an array of `{name, value, ...}` objects (schema builder); confirm by hitting `/api/v1/customformat/schema` before writing an API client (not verified against a live server).
- Field payloads: Title/Group: `{"value": "<regex>"}` (.NET regex, **always case-insensitive**, [RegexSpecificationBase.cs](https://raw.githubusercontent.com/Lidarr/Lidarr/develop/src/NzbDrone.Core/CustomFormats/Specifications/RegexSpecificationBase.cs)). Size: `{"min": n, "max": n}` in **GB** of the whole release, match is `min < size <= max`, validator requires `max > min` ([SizeSpecification.cs](https://raw.githubusercontent.com/Lidarr/Lidarr/develop/src/NzbDrone.Core/CustomFormats/Specifications/SizeSpecification.cs)). Indexer flag: `{"value": <int flag enum>}`.
- Spec combination: within one format, the wiki says specs AND unless Negate/Required altered ([settings.md](https://raw.githubusercontent.com/Servarr/Wiki/master/lidarr/settings.md)). The TRaSH/Sonarr convention is: non-required specs OR together, `required: true` specs AND. The Servarr "Preferred Groups" example (three non-required group specs meant to match any one) only works under the OR semantic, so the wiki prose is likely imprecise. Treat OR-of-non-required, AND-of-required as the working model; not verified in `CustomFormatCalculationService` (not fetched).
- TRaSH files are Radarr/Sonarr JSON of the same shape but use `"fields": {"value": ...}` and `ReleaseTitleSpecification`, `ReleaseGroupSpecification`, `SourceSpecification`, `ResolutionSpecification` etc. Lidarr rejects arr-video-only implementations. If we import TRaSH JSON, whitelist the four implementations above.

**1.2 Quality definitions: units and logic.** [Servarr settings](https://raw.githubusercontent.com/Servarr/Wiki/master/lidarr/settings.md): "size limits use kilobits per second (kbps)... Lidarr computes a bitrate from the file size and duration." Code ([AcceptableSizeSpecification.cs](https://raw.githubusercontent.com/Lidarr/Lidarr/develop/src/NzbDrone.Core/DecisionEngine/Specifications/AcceptableSizeSpecification.cs)):
- Release size 0 (unknown): **accept**, skip check.
- Min bound set (`MinSize` non-null) or max bound set non-zero, and album duration 0: **reject** "Album duration is 0, unable to validate size until it is available".
- Min compares against the **shortest** monitored release duration, max against the **longest**, summed across albums in the grab. This is Lidarr's answer to editions with different lengths.
- Max `null` or `0` = unlimited. `Min` 0 = no lower bound (a `0` min is still non-null, so the duration-0 reject still fires for lossy where max > 0).
- Conversion: kbps x duration seconds x 1000/8 bytes is assumed. The `Kilobits()` extension body was not retrievable, so decimal vs 1024 base is **not verified**; difference is 2.4%.

Lidarr default definitions, group names and weights are in [Quality.cs](https://raw.githubusercontent.com/Lidarr/Lidarr/develop/src/NzbDrone.Core/Qualities/Quality.cs) (table A.1 comparison column). Selected rows [S]: FLAC/ALAC/APE/WavPack/FLAC 24/ALAC 24: 0/895/null; MP3-320, AAC-320, MP3 VBR (V0), AAC-VBR: 0/195/350; MP3-256, V2, AAC-256, Vorbis Q8: 0/95/280; MP3-192, AAC-192, Vorbis Q6: 0/95/210; MP3-128: 0/95/140; Unknown and WMA: 0/195/350 (Unknown 0/195/350).

**1.3 Quality ordering vs custom format score** ([UpgradableSpecification.cs](https://raw.githubusercontent.com/Lidarr/Lidarr/develop/src/NzbDrone.Core/DecisionEngine/Specifications/UpgradableSpecification.cs), [CustomFormatAllowedByProfileSpecification.cs](https://raw.githubusercontent.com/Lidarr/Lidarr/develop/src/NzbDrone.Core/DecisionEngine/Specifications/CustomFormatAllowedByProfileSpecification.cs)):
- **Min format score**: a candidate with score < `MinFormatScore` is permanently rejected at decision time.
- **Quality dominates.** `IsUpgradable`: higher quality = upgrade (score ignored); lower quality = downgrade, rejected; equal quality: upgrade only if new score > current score (strictly).
- **Cutoff** (`CutoffNotMet`): keep searching if current quality is below cutoff, **or** if current custom-format score < `CutoffFormatScore` ("upgrade until"). Once both are satisfied the item stops being "wanted". If `UpgradeAllowed` is off, the cutoff is effectively the first allowed quality; and when both a quality or score improvement exist but upgrades are off, the upgrade is blocked.
- Consequence: scores are tie-breakers within a quality tier and the way to prefer CD over WEB inside "FLAC 16bit". They cannot make a MP3 320 beat a FLAC. Upgrade-until > 0 with Min CF 0 means a FLAC with score 0 keeps being "wanted" until a higher-scoring FLAC arrives.
- Adding a custom format auto-adds it to all profiles at score 0; deleting the last one resets min and cutoff scores to 0.
- Revisions (PROPER/REPEAT) are a separate axis; skip for music.

**1.4 Release profiles.** Fields: Required, Ignored, Indexer, Tags, Enabled ([ReleaseProfile.cs](https://raw.githubusercontent.com/Lidarr/Lidarr/develop/src/NzbDrone.Core/Profiles/Releases/ReleaseProfile.cs); the wiki still documents Preferred scoring terms, but the current model has none, so scoring has moved to custom formats). Evaluation ([ReleaseRestrictionsSpecification.cs](https://raw.githubusercontent.com/Lidarr/Lidarr/develop/src/NzbDrone.Core/DecisionEngine/Specifications/ReleaseRestrictionsSpecification.cs)): for each profile with Required terms, the title must contain **at least one** (OR); for each profile with Ignored terms, **any** match rejects. `IndexerId` 0 = all indexers. Term parsing ([PerlRegexFactory.cs](https://raw.githubusercontent.com/Lidarr/Lidarr/develop/src/NzbDrone.Core/Profiles/Releases/PerlRegexFactory.cs), [TermMatcherService.cs](https://raw.githubusercontent.com/Lidarr/Lidarr/develop/src/NzbDrone.Core/Profiles/Releases/TermMatcherService.cs)): a term of form `/pattern/modifiers` is a .NET regex, anything else is a case-insensitive substring. Matching is against the **release title**, not the folder contents.

**1.5 Delay profiles** ([DelaySpecification.cs](https://raw.githubusercontent.com/Lidarr/Lidarr/develop/src/NzbDrone.Core/DecisionEngine/Specifications/RssSync/DelaySpecification.cs), [DelayProfile.cs](https://raw.githubusercontent.com/Lidarr/Lidarr/develop/src/NzbDrone.Core/Profiles/Delay/DelayProfile.cs)): delay is per protocol (minutes); first item in `Items` is the preferred protocol; manual (user-invoked) searches ignore delay; delay 0 accepts immediately; for the preferred protocol a revision upgrade, "BypassIfHighestQuality" (candidate >= best allowed quality) or "BypassIfAboveCustomFormatScore" (score >= minimum) skip the wait; otherwise the release is rejected until `AgeMinutes >= delay` or the oldest pending release for that album has aged past the delay. Tags select the profile via `BestForTags`; untagged = default.

**1.6 Default quality profiles and metadata profile**: code excerpts in A.2, A.6 ([QualityProfileService.cs](https://raw.githubusercontent.com/Lidarr/Lidarr/develop/src/NzbDrone.Core/Profiles/Qualities/QualityProfileService.cs)). Metadata profile has three axes (primary types, secondary types, release statuses), not just the five checkboxes the wiki lists.

**1.7 Title parsing**: Lidarr's parser only detects 24-bit (`24[-._ ]?bit`, `flac24`, `TR24`, `24-(44|48|96|192)`) and a few bitrates; it does **not** distinguish 24/96 from 24/192 and has no source field ([QualityParser.cs](https://raw.githubusercontent.com/Lidarr/Lidarr/develop/src/NzbDrone.Core/Parser/QualityParser.cs)). Hi-res tiers were requested and closed "not planned" ([issue #4153](https://github.com/Lidarr/Lidarr/issues/4153)). Our own FLAC 24 can add sample-rate custom formats if we parse them.

**1.8 TRaSH-style data**: none exists; confirmed by the page text and the repo listing (header of this doc). The only structured community defaults are the two lists in A.3: Servarr tips ([tips-and-tricks.md](https://raw.githubusercontent.com/Servarr/Wiki/master/lidarr/tips-and-tricks.md)) and Tubifarry's SoulSeek+Lidarr guide ([discussion #138](https://github.com/TypNull/Tubifarry/discussions/138), min score 1, cutoff Lossless, order FLAC 24 > FLAC/ALAC > 320).

### 2. Typical bitrates (basis for size-per-length)

| Format | Typical average | Source | Status |
|---|---|---|---|
| FLAC 16/44.1 | raw PCM 1411 kbps; compressed ~700-1100, Lidarr pref 895 | [Lidarr Quality.cs](https://raw.githubusercontent.com/Lidarr/Lidarr/develop/src/NzbDrone.Core/Qualities/Quality.cs); "~1000 kbps" in search snippet | pref [S]; range [D] (50-80% of raw) |
| FLAC 24/96 | raw 4608; compressed ~2500-3200; one source "typically below 3000, could be 800-5000" | derived; weak snippet: [Audiophile Style](https://audiophilestyle.com/forums/topic/10215-flac-2496-playing-at-3072-kbps/) (page blocked, search snippet only) | [D] |
| FLAC 24/192 | raw 9216; compressed ~4500-6500 | derived (55-70% of raw); issue #1146 says "minimum would be 4000kbps" ([#1146](https://github.com/lidarr/Lidarr/issues/1146)) | [D], loosely corroborated |
| ALAC | similar to FLAC (+/- 5%) | assumption | [D], unverified |
| MP3 CBR 320 | 320 exact | definition | [S] |
| MP3 V0 | target ~240-245, typical 220-260 | [Hydrogenaudio LAME](https://wiki.hydrogenaudio.org/index.php?title=LAME) (page behind Cloudflare, figures via search snippet) | [S] indirect |
| MP3 V2 | target ~190, typical 170-210 | same | [S] indirect |
| AAC 256 | iTunes Plus is AAC-LC 256 kbps; Lidarr max 280 | search snippets; [Quality.cs](https://raw.githubusercontent.com/Lidarr/Lidarr/develop/src/NzbDrone.Core/Qualities/Quality.cs) | partial |
| Opus | storage 96-128 kbps; "128 VBR pretty much transparent" | [Xiph Opus Recommended Settings](https://wiki.xiph.org/Opus_Recommended_Settings) | [S] |

Recommended ratio for hi-res: bound with `kbps` max ~ raw PCM rate (sample_rate x bit_depth x channels), because lossless cannot exceed that except for noise-like content. FLAC 24 max 9500 therefore covers up to 24/192 stereo. A 24/192 max rejects >2-channel releases and DSD; those are out of scope [O].

### 3. Community conventions

**3.1 RED/OPS-style (documented publicly):**
- Only FLAC allowed as lossless; rips from commercially pressed CD; "Never transcode your lossy files to FLAC, 320 or V0": [RED rules mirror](https://interviewfor.red/en/rules.html), [formats](https://interviewfor.red/en/formats.html).
- Min lossy bitrate 192 CBR or V2 VBR, allowed lossy MP3/AAC/AC3/DTS: [formats](https://interviewfor.red/en/formats.html). This supports our quality floor of MP3 192 and V2.
- Log/cue: FLAC without an EAC/XLD log can be trumped by one with; 100% logs trump lower scores: [rules](https://interviewfor.red/en/rules.html).
- Perfect FLAC on RED = "100% log for CD, or any Vinyl/DVD/Soundboard/WEB/Cassette/Blu-ray/SACD/DAT". Log, cue and 100% apply to **CD only**; a naive "must contain log" would reject all WEB: [autobrr #1688](https://github.com/autobrr/autobrr/discussions/1688). Implication: do **not** put "log" or "cue" in Must Contain; award a CF bonus for CD + `100%` instead.
- The interviewfor.red pages do not mention upconverts, "lossy master", MQA, vinyl or WEB rules. Those RED terms are only attested via the autobrr thread (media list, "Lossy Master" implied as an exclusion). Not verified at the primary RED wiki.

**3.2 Lidarr community:**
- Vinyl rips from some uploaders arrive as one file per LP side or `.wvp`, which Lidarr cannot import ([haynesnetwork #610](https://github.com/thaynes43/haynesnetwork/issues/610), v3.1.6). Reject via release profile naming the uploader. This supports a vinyl penalty independent of audio quality.
- `.wv` WavPack is recognised; `.wvp` is not (same issue).
- The Servarr tip explicitly suggests tightening FLAC max to filter single-file CUE+FLAC rips.
- Reddit r/Lidarr and r/trackers were **not directly fetched** (blocked/not returned by the search tool); the Servarr FAQ links one r/Lidarr post but only for folder layout. No Reddit-sourced reject-term or release-group ranking is claimed.

**3.3 MQA**: lossy by design ("MQA encoding is lossy", folded high band, as few as 13 effective PCM bits without decoder): [Wikipedia](https://en.wikipedia.org/wiki/Master_Quality_Authenticated). Basis for A.4 reject.

**3.4 Release groups**: Servarr lists DeVOiD, PERFECT, ENRiCH ([tips](https://raw.githubusercontent.com/Servarr/Wiki/master/lidarr/tips-and-tricks.md)); Tubifarry adds BigFLAC, GalaxyLossless, MusiCHI, LoRD ([#138](https://github.com/TypNull/Tubifarry/discussions/138)). I found **no independent ranking** of music groups; DeVOiD merely exists as a FLAC scene group ([sceneflac blog](https://sceneflac.blogspot.com/2020/03/flac-devoid.html), [ReScene groups](http://rescene.wikidot.com/groups) in search results, not fetched). Treat the group list as a weak "known scene groups" signal. Private trackers key on uploader and log score rather than groups, and Lidarr's `ReleaseGroup` parse only fires on `-GROUP` title suffixes.

**3.5 Remaster/Deluxe handling**: Tubifarry gives Remastered +10, Deluxe +5, Hi-Res +15. These influence which **edition string appears in the release title**, but Lidarr's import matches files to a MusicBrainz release by tags/durations and format is a weak signal (weight 1.0) ([Servarr FAQ](https://raw.githubusercontent.com/Servarr/Wiki/master/lidarr/faq.md)). Preferring "Remastered" at grab time can pull in a different master than the user's monitored release; scoring them positive is a taste decision, so we default to 0 [O].

**3.6 24-bit vinyl trade-off [O]**: vinyl ripped at 24/96 is a needle-drop; it is large (hi-res bloat) and not inherently better than the CD master, and one-file-per-side makes it unimportable by track-matching. Hence soft penalty (-50) rather than a ban; Servarr's -10000 is effectively a ban when Min CF is 1.

### 4. Pitfalls

- **Unknown duration**: Lidarr rejects when a size bound applies and the duration is 0 (MusicBrainz tracks with no length), and accepts when the release size is unknown ([AcceptableSizeSpecification.cs](https://raw.githubusercontent.com/Lidarr/Lidarr/develop/src/NzbDrone.Core/DecisionEngine/Specifications/AcceptableSizeSpecification.cs), [Servarr FAQ](https://raw.githubusercontent.com/Servarr/Wiki/master/lidarr/faq.md) "Album duration is 0"). Recommendation [O]: do not copy the hard reject. Fall back to a track-count heuristic (e.g. 3.5 min/track) with a flag, or skip the size check and mark the decision "unverified size".
- **Multiple editions/discs**: Lidarr uses min duration for the lower bound and max duration for the upper, summed over albums. Multi-disc: total duration is the sum, so kbps stays sane. Compilation grabs of multiple albums sum durations too.
- **Classical/long albums**: bitrate is duration-normalised, so length is fine; the risk is dynamic range. Quiet/solo classical FLAC compresses well below 895 kbps (could reach 400-600 [D]), so a non-zero FLAC min would reject legitimate releases. Lidarr uses min 0 (our A.1 does too).
- **Hi-res bloat**: Lidarr's 1500 UI cap and Servarr's 1495 FLAC 24 max reject any genuine hi-res release; they were designed for CUE-image filtering. Our 9500 max accepts everything up to 24/192 stereo, so single-file CUE detection must come from a custom format (A.4), not from size.
- **Embedded art / extras inflate size**: MP3 320 releases with big cover art can read >320 kbps (hence max 350 and min 290).
- **VBR lossy minima**: a quiet V0 album can average well below 220 [D]; keep V0 min conservative.
- **Quality parsing gaps**: Lidarr treats many `.m4a/.ogg/.opus` as Unknown ([FAQ](https://raw.githubusercontent.com/Servarr/Wiki/master/lidarr/faq.md) lines 184-188). We should classify AAC/Opus ourselves, not rely on Lidarr's parser.
- **CF score pitfalls**: Min CF > 0 rejects untagged releases (Servarr's min of 1 plus a "Lossless" format means a release without the word in its title is never grabbed). Equal-quality upgrades need a strictly higher score. Large negative scores combined with a positive min can make a profile unsatisfiable.

---

## C. Open questions

1. Decimal (1000) vs binary (1024) kilobit in size math: confirm in a live Lidarr or in `Int64Extensions` (file not retrievable).
2. Spec combination semantics: confirm AND/OR from `CustomFormatCalculationService` (not fetched); I assumed required-AND, non-required-OR.
3. API shape of `fields` (object vs array): inspect `/api/v1/customformat/schema`.
4. Verify hydrogenaudio V0/V2 figures at the primary page (Cloudflare-blocked here); numbers come from search-engine excerpts.
5. Reddit (r/Lidarr, r/trackers), RED/OPS primary wiki and Servarr Discord summaries: no direct fetch succeeded. The A.4 reject list and group ranking are therefore **ours**, not community-validated.
6. Decide product policy: unknown album duration (reject vs accept-flagged), hi-res default (FLAC 24 above FLAC 16 in cutoff order?), and whether Vinyl is penalised (-50) or banned.
7. Do we expose ALAC/WAV/Opus as qualities? Opus parse and bounds (96-128 pref) are only in the table, not in our quality enum.
8. Delay profile protocol model: Trackseerr sources are not Usenet/Torrent; define protocol items (Soulseek, Deezer, Torrent, Usenet).
