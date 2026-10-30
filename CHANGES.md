# ISP Monitor v6.1 — what changed from v6.0

- **Verdict strip seeds on startup.** Opening the app used to leave the
  top strip on its placeholder until the first new test ran, even when
  the log already held tests. Now it shows the most recent logged test
  right away. The log gains a verdict column (auto-migrated, old rows
  kept); rows written before this version show a layer + flags fallback
  like "ISP issue (SLOW)" instead of the full verdict sentence.

# ISP Monitor v6.0 — what changed from v5.3

- **Packet loss %.** Every TEST NOW runs 10 extra probes and reports
  loss. 20%+ flags LOSS — the number ISPs take seriously for calls
  and games — and names the ISP as the fault when latency is otherwise
  healthy.
- **WiFi signal %.** On WiFi, the signal % now shows on the WiFi card
  (Windows). Below 40% the test names weak signal as the fault.
- **Separate DNS check.** A DNS test of its own, so "DNS is broken"
  is no longer misread as "internet is broken". A DNS fault names
  switching to 8.8.8.8 / 1.1.1.1.
- **Top verdict strip.** The latest verdict sits at the very top of
  the window — healthy / issue, speed, loss, signal — visible without
  scrolling.
- **Downtime totals, outage windows, slowest hours.** The text report
  now totals downtime (~minutes across outages), lists each outage
  window with start/end/duration, and names your slowest hours. The
  summary also reports average packet loss.
- **Copy ISP summary button.** One click puts a short complaint-ready
  summary on the clipboard — paste it straight into ISP chat.
- **Start with Windows.** A Settings checkbox adds the app to the
  Windows Run key (removes it when unchecked). A full minimize-to-tray
  icon is deferred: it needs a new dependency and is not in this build.
- **Outage alert.** A red OUTAGE label appears in the top bar on an
  outage result and clears on recovery. A sound + taskbar flash fire
  only when an outage *starts*, not every 5 minutes while one drags on.
  The sound can be switched off in Settings.
- **Light theme.** Dark (default) or Light in Settings; the choice
  applies on the next launch, since every widget reads its colors
  when it is built.
- **Sortable log, newest first.** Click any log column header to sort
  (click again to reverse). Fixed a real bug: the old code displayed
  oldest first despite Help saying newest first. The visible table
  also caps at the latest 2000 rows for speed; the full history stays
  in the CSV and in reports.
- **Chart plan line + downsampling.** A dashed line marks your plan
  speed on the chart (when a plan is set). Long histories downsample
  to 600 plotted points so the chart stays fast after months of tests.
- **First-run hint.** New users with no log yet see a one-line
  getting-started hint, dismissible, remembered across launches.
- **Remembers window size and position.** Saved on close, restored on
  launch, clamped to the current screen.
- **CSV migration.** The log gains loss_pct, wifi_signal, and dns_ms
  columns. Old logs are migrated automatically on first launch — rows
  preserved, new columns empty — so existing history keeps working.
- **GitHub set.** README.md, BUILD_EXE_MONITOR.bat, ISP_Monitor.spec,
  and a screenshot now live next to the source, ready to push.

# ISP Monitor v5.3 — what changed from v5.2

- **Loading spinner in the top bar.** A test takes 10–60 seconds, but
  all the old progress feedback lived at the bottom of a scrollable
  page — invisible from the buttons. Now an animated spinner plus
  "Testing…" / "Measuring line speed…" sits next to the buttons while
  anything runs, so a test never looks dead.
- **No more silent double-click bug.** TEST NOW and LINE SPEED disable
  while any test runs. Previously a click during another run was
  dropped with no message at all, which is why LINE SPEED seemed to
  need two clicks. Buttons re-enable the moment the run finishes, and
  a failed run can no longer leave them stuck disabled.

# ISP Monitor v5.2 — what changed from v5.1

- **No more blinking.** Every helper tool the app shells out to (ping,
  tracert, netsh, ipconfig — about 15 calls per TEST NOW) now runs with
  its console window hidden, so tests run silently. The flashing
  terminals were never the app itself (the .exe is built windowed);
  they were one console popup per helper call.
- **Help updated:** new "What's new in v5.1 & v5.2" section, TEST NOW
  timing corrected (~10–20 sec, plus a traceroute when flagged), LINE
  SPEED now notes upload + peak logging, routine download test notes
  its 8-second window, and the stale "TEST NOW shows ~20 Mbps" line in
  the capacity panel is now a general statement instead of a number.

# ISP Monitor v5.1 — what changed from v5

- **The window scrolls now.** The whole page lives in a scrollable area
  with a scrollbar on the right and mouse-wheel support, and the window
  opens sized to your screen instead of a fixed 860px. Previously the
  page was ~1200px tall with no scrollbar at all, so on a shorter screen
  the chart, log table, and status bar were simply unreachable.
- **Help window fix:** help used to grab the mouse-wheel binding for the
  whole app and drop it on close, which could kill main-window
  scrolling. It now binds only to its own window.
- The log table keeps its own scrollbar: wheel over the table scrolls
  the table, wheel anywhere else scrolls the page.

# ISP Monitor v5 — what changed from v4

Drop-in replacement: same filename (`isp_monitor.py`), same build
(`BUILD_EXE_MONITOR.bat`), same dependencies. Your existing `isp_log.csv`
keeps working — headers are unchanged. New files are separate:
`peak_log.csv`, `trace_log.txt`, `isp_config.json` (created on first save).

## The gaps, fixed

1. **Real ping, not just TCP connects.** Tests now try a real ICMP ping
   first and fall back to TCP connects when ICMP is blocked, so a router
   with closed web ports no longer reads as no-response.
2. **Traceroute actually pinpoints the failing hop.** When a test flags a
   problem (or on every manual TEST NOW), it traces the route to the CDN
   and names the hop where traffic dies or slows — saved to
   `trace_log.txt`. Healthy monitoring cycles skip the trace so the loop
   stays fast. Deliberately conservative: a route that answers nothing
   is reported as inconclusive, not blamed on the router.
3. **Routine speed test measures real throughput.** The old one-pass
   ~1 MB test finished in under 0.1 s on a 500 Mbps line and mostly
   measured connection setup. It now runs up to 8 seconds / 25 MB
   (still single-stream, so it still reflects real-world downloads).
4. **LINE SPEED now measures upload too, and its results are logged.**
   Every LINE SPEED run appends to `peak_log.csv` and the text report
   includes a LINE SPEED section — so capacity evidence is in the file
   you send your ISP.
5. **Your plan speed is now a setting.** A settings row under the
   filters lets you enter plan speed, the slow-alert % of plan
   (default 50%), and the check interval (default 5 min). SLOW now
   flags when you fall below your % of plan instead of only below a
   fixed 10 Mbps. Saved to `isp_config.json`; applies to the next test.
6. **Small fix:** an existing but empty `isp_log.csv` no longer loses
   its header row.

## What v5 deliberately does NOT change

- No upload test in the routine 5-minute loop (LINE SPEED covers it).
- No background service: it still only watches while the PC is on and
  the app is open, and checks at your set interval — so a blip shorter
  than the interval can still be missed.
- Diagnosis is still layered inference plus traceroute, not a
  per-device probe — it cannot see inside your modem or ISP's hardware.

## Verified

27 unit tests pass (ICMP parsing in Windows + Linux formats, TCP
fallback when ICMP is blocked, tracert/traceroute parsing, hop-break
and bottleneck diagnosis, plan-aware thresholds, config roundtrip,
empty-CSV header fix, report contents). GUI was not run here — on your
Windows PC, replace `isp_monitor.py`, re-run `BUILD_EXE_MONITOR.bat`,
and the new settings row appears under the filters.
