# Sumář pro Linux-Clauda — 2026-07-24, večer

## TL;DR
Ten chronický latency bug (~370ms baseline + narůstání až na ~900ms za ~80 min běhu) je **vyřešený a nasazený** jako **v1.0.1**. Obě dvě zdánlivě oddělené příčiny (vysoká baseline latence a její růst v čase) byly ve skutečnosti **jeden a ten samý bug**.

## Root cause
`server/audio_engine.py`, `AudioCaptureThread.run()` — read loop z `parec` stdout čet jen max jeden packet (`chunk_bytes`) za iteraci. Když PipeWire při připojení `parec` k monitor sourcu jednorázově vydal víc dat najednou (burst), ten přebytek se nikdy nedrenoval pryč — zůstal jako **trvalý fázový posun** v `read_buffer`.

Potvrzeno přímou instrumentací (log do `/tmp/etherwave_server_diag.log` po 0.5s): `read_buffer` držel stabilně **11-12 pending chunků (~55-60ms)** donekonečna, i přes to, že `behind_schedule_ms` bylo stabilně ~0 (odesílací plán NEbyl pozadu). Existující catch-up větev (`elif next_send_time < now - packet_duration`) proto nikdy nezafungovala — ta řeší "jsme pozadu za časem", ne "data jsou stará, i když čas sedí".

## Fix (dva kroky, oba potřeba)
1. `chunk = proc.stdout.read(chunk_bytes * 16)` místo `read(chunk_bytes - len(read_buffer))` — umožní natáhnout celý backlog z roury najednou místo max 1 chunku/iteraci.
2. Nová `BACKLOG_TRIM_THRESHOLD_CHUNKS = 3` logika: po každém readu, pokud `read_buffer` obsahuje víc než 3 pending chunky, zahodí starší přebytek a nechá jen nejnovější chunk, + resetuje `next_send_time = time.perf_counter()`. Klientův jitter buffer to vstřebá stejně jako běžný síťový výpadek (malá mezera v seq číslech).

## Naměřeno (dvě nezávislé metody, souhlasí)
- Mikrofon (BlackHole loopback, trigger→slyšitelný zvuk): **~65ms** (3× opakováno, konzistentní)
- Přímo z UDP paketů (bypass audio hardware úplně): **~41ms** server-side (trigger→paket na drátu), 2× opakováno po ~33 min uptime, **žádný růst**
- Rozdíl (~65 - ~41 = ~24ms) sedí s klientským jitter bufferem (15ms) + CoreAudio output latency (~12-27ms, teď měřeno a zobrazeno v UI)

## Co NEpomohlo (zkoušeno, změřeno, revertnuto)
- PipeWire clock/quantum force (`pw-metadata clock.force-rate/force-quantum`) — ~356ms vs ~369ms, zanedbatelné. Kód úplně odstraněn, nebyl důvod ho tam nechávat.

## Vedlejší změny v tomhle release
- Client: `AudioOutputStream.output_latency_ms` — reálná (ne požadovaná) CoreAudio output latency teď přičtená k jitter buffer depth v UI.
- Oba apps: stats display přepsaný na fixed-width, vystředěnou grid tabulku (dřív to bylo řádkové skákání textu).
- Verze: po zveřejnění repa zůstal na GitHubu jen tag v1.0.0 (staré v1.0.1-v1.0.12 z interní historie smazané). Tenhle fix je nově otagovaný a pushnutý jako v1.0.1. Pozor příště: `git fetch --prune-tags` než budeš bumpovat verzi, lokální tagy mohly být zastaralé (přesně tahle chyba se mi dnes stala).

## Stav nasazení
- Server: `etherwave-server.service` (systemd --user) na CachyOS, v1.0.1, běží a stream aktivní.
- Client: `.app` nainstalovaná v `/Applications` na macOS, v1.0.1.
- Vše commitnuté a pushnuté na `main`, tag `v1.0.1` pushnutý.

## Diagnostické skripty (pokud budeš chtít znovu měřit)
- Nepoužívej mikrofonní onset detekci bez ~1s "arm delay" po startu streamu (fixní startup transient) a bez ověření, že na zdrojovém stroji nic jiného nehraje (Spotify nás jednou zmátl).
- Radši měř přímo přes raw UDP packet sniff (`socket.SO_REUSEPORT`, bind na port 51235, parsuj header `!4sIdBH`, počítej RMS payloadu) — nezávislé na audio hardwaru, spolehlivější.

---
*Tenhle soubor je jednorázová handoff poznámka, klidně ho smaž po přečtení.*
