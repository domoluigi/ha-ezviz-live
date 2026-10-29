# EZVIZ Live per Home Assistant

Integrazione di supporto a quella ufficiale **EZVIZ** per i dispositivi **senza RTSP locale** (spioncini e campanelli a batteria, telecamere a batteria). Aggiunge:

- **diretta su richiesta** via cloud EZVIZ, decifrata in Home Assistant: il dispositivo si sveglia solo mentre qualcuno guarda;
- **foto dell'ultimo evento** decifrata (anche con la crittografia del video attiva);
- **eventi in tempo reale** dalle notifiche push EZVIZ (in pochi secondi, invece del polling ogni 30 s).

Non sostituisce né modifica l'integrazione ufficiale `ezviz`: le due convivono.

> Integrazione non ufficiale, non affiliata a EZVIZ. Provata su uno spioncino **CP4** (firmware V5.3.3) con Home Assistant 2026.10. Usa la libreria [pyezvizapi](https://github.com/RenierM26/pyEzvizApi) 1.0.5.0, la stessa dell'integrazione ufficiale.

## Installazione

**HACS** → menu ⋮ → *Repository personalizzati* → URL `https://github.com/domoluigi/ha-ezviz-live`, tipo *Integrazione* → installa → riavvia Home Assistant.

**Manuale**: copiare `custom_components/ezviz_live` in `/config/custom_components/` e riavviare.

Poi *Impostazioni → Dispositivi e servizi → Aggiungi integrazione → EZVIZ Live*: email e password dell'account dell'app EZVIZ (ed eventuale codice di verifica). **La password non viene salvata**: si conserva solo il token di sessione; se scade, Home Assistant chiede di accedere di nuovo.

Richiede Home Assistant **2026.10** o successivo.

## Entità (per ogni dispositivo con video)

| Entità | Cosa fa |
|---|---|
| Camera *Live* | Diretta HEVC con audio, servita allo stream di Home Assistant (HLS) e a go2rtc. L'immagine statica è la foto dell'ultimo evento: aprire la card non sveglia il dispositivo |
| Camera *Compatibile (MJPEG)* | La stessa diretta come MJPEG (5 fps, 960 px, senza audio), per i browser che non avviano da soli il video HEVC (per esempio un tablet a parete con WebView Android) |
| Image *Foto ultimo evento* | Foto dell'ultimo allarme, decifrata |
| Event *Allarme* | Un evento per ogni nuovo allarme (`motion`, `doorbell`, `other`), con ora, tipo, ritardo e origine (`push`/`poll`) negli attributi |
| Binary sensor *Diretta attiva* | Acceso mentre il dispositivo trasmette (e consuma batteria) |

**Opzioni** (*Configura*): durata massima della diretta (predefinita 300 s).

## Come funziona la diretta

- Il cloud EZVIZ inoltra il video del dispositivo (protocollo VTM). Il contenuto è cifrato: la chiave del dispositivo si ottiene dal cloud con lo stesso account, quindi **la crittografia può restare attiva** e non serve il codice di verifica stampato sull'etichetta.
- La decifratura avviene **man mano che arrivano i dati**, ai confini sicuri dei pacchetti MPEG-PS: il risultato è identico byte per byte a quello ottenuto decifrando la registrazione intera.
- **Una sola sessione per dispositivo**: un dispositivo a batteria regge una diretta alla volta (due sessioni contemporanee si bloccano a vicenda). L'integrazione apre una sessione e la condivide fra tutti gli spettatori (HLS, go2rtc, MJPEG); ognuno ha il suo `ffmpeg` (solo rimux, nessuna ricodifica per l'HEVC).
- La sessione si chiude **3 s dopo** che l'ultimo spettatore ha chiuso, oppure alla **durata massima**. Dopo il limite le nuove richieste sono rifiutate per 2 minuti, così i ritentativi automatici dei player non tengono sveglio il dispositivo.
- Se il cloud chiude la sessione mentre qualcuno guarda (un CP4 lo fa dopo circa due minuti), viene riaperta da sola: si vede un breve blocco del video. Dopo tre segmenti troppo corti di fila l'integrazione rinuncia, per non svegliare il dispositivo a ripetizione.
- Tempi misurati su un CP4: primo dato dal dispositivo in 4–6 s (risveglio), primo byte dopo ffmpeg 1,1 s più tardi, video sul telefono in circa 5 s (HLS). L'analisi dell'ingresso di ffmpeg è limitata a 1 s: con il valore predefinito (5 s) il video arrivava dopo circa 8 s.
- Il flusso interno è servito solo a indirizzi locali, con un codice d'accesso casuale che cambia a ogni avvio.

## Eventi

Le notifiche push EZVIZ arrivano in circa 5 s e fanno rileggere subito l'elenco degli allarmi; un controllo ogni 60 s fa da riserva se il collegamento push cade (ogni 10 minuti viene verificato e, se è caduto, riaperto). Ogni allarme genera un solo evento, anche se arriva sia via push sia via controllo.

Il cloud classifica gli eventi dei dispositivi PIR come *PIR Alarm* senza distinguere le persone: per un avviso "persona alla porta" si può passare la foto dell'evento a un'attività AI di Home Assistant (`ai_task.generate_data` con l'entità *Foto ultimo evento* come allegato).

## Limiti noti

- Funziona solo via cloud EZVIZ (serve Internet).
- Provata su un solo modello (CP4). Dispositivi con cifratura di trasporto VTM (ECDH) non sono supportati: la diretta si interrompe con un avviso nel log.
- WebRTC: go2rtc riceve il flusso, ma i browser che non accettano HEVC in WebRTC ripiegano sull'HLS.

## Sviluppo

- `pytest` esegue i test della sessione condivisa con un dispositivo finto (vedi `tests/`).
- `tools/make_brand.py` rigenera le icone in `custom_components/ezviz_live/brand/`.

Licenza MIT.
