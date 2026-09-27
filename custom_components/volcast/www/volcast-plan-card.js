/**
 * Volcast Plan Card — kokpit magazynu energii na dashboardzie HA.
 *
 * Karta jest dostarczana RAZEM z integracją i rejestrowana automatycznie, więc
 * użytkownik nie wgrywa niczego ręcznie ani nie dodaje zasobu w Lovelace.
 *
 * Styl: paleta aplikacji Volcast (styles/theme.ts w aplikacji) — te same tokeny,
 * żeby wszystko, co widzi klient, wyglądało jak jedna rzecz.
 *
 * SKĄD DANE:
 *   - wartości BIEŻĄCE czytamy WPROST z encji falownika (`hass.states`) — lokalnie,
 *     bez chmury. Kokpit żyje nawet przy zerwanym łączu.
 *   - PLAN przychodzi z chmury przez `get-schedule` i siedzi w atrybutach sensora.
 *   - prognoza SoC liczona jest TUTAJ, z bieżącego odczytu i mocy per slot.
 *     Zakotwiczenie w realnym SoC (a nie w projekcji sprzed doby) sprawia, że
 *     krzywa sama się koryguje przy każdym odświeżeniu.
 *
 * Kolumny i krzywa dzielą jeden SVG — inaczej oś godzinowa rozjeżdżałaby się
 * między warstwami przy dowolnej szerokości karty.
 */

const PALETA = {
  canvas: '#0B1120',
  surface: '#111827',
  surface2: '#1F2937',
  border: 'rgba(255,255,255,0.06)',
  borderMid: 'rgba(255,255,255,0.10)',
  primary: '#34D399',
  primaryBright: '#6EE7B7',
  violet: '#A78BFA',
  amber: '#FBBF24',
  sky: '#60A5FA',
  orange: '#FB923C',
  danger: '#F87171',
  textPrimary: '#F9FAFB',
  textSecondary: '#9CA3AF',
  textMuted: '#6B7280',
};

/** Słownik tekstów widocznych dla użytkownika. Język bierzemy z `hass.language`
 *  (każdy wariant `pl*` to polski, wszystko inne — angielski). */
const I18N = {
  pl: {
    no_entity: 'Brak encji',
    no_plan: 'Brak planu z chmury',
    control_on: 'Sterowanie włączone',
    control_off: 'Sterowanie wyłączone',
    control_paused_account: 'Sterowanie wstrzymane przez konto',
    control_no_cloud: 'Sterowanie wstrzymane — brak kontaktu z chmurą',
    control_paused_foreign: 'Wstrzymane — ktoś zmienił ustawienia falownika',
    control_trial: 'Tryb próbny (bez zapisu)',
    soc: 'SoC', pv: 'PV', home: 'Dom', battery: 'Bateria', grid: 'Sieć',
    import: 'import', export: 'eksport',
    soc_forecast: 'Prognoza SoC',
    soc_forecast_hint: 'Prognoza (przed: godziny minione)',
    price_this_hour: 'Cena tej godziny',
    plan_until: 'Plan do',
    power_unset: 'moc niezadana',
    plan_target: 'cel planu',
    source: 'źródło',
    purpose: 'cel',
    export_blocked: 'eksport zablokowany',
    past_hour: 'godzina miniona',
    from_measurement: 'od pomiaru',
    self_consume: 'Autokonsumpcja', export_pv: 'PV → sieć',
    charge_from_pv: 'Ładuj z PV', charge_from_grid: 'Ładuj z sieci',
    discharge_self: 'Bateria → dom', discharge_sell: 'Bateria → sieć',
    idle: 'Bezczynność', grid_import: 'Import z sieci',
  },
  en: {
    no_entity: 'Entity missing',
    no_plan: 'No plan from the cloud',
    control_on: 'Control on',
    control_off: 'Control off',
    control_paused_account: 'Control paused by account',
    control_no_cloud: 'Control paused — no contact with cloud',
    control_paused_foreign: 'Paused — inverter settings were changed by someone else',
    control_trial: 'Trial mode (no writes)',
    soc: 'SoC', pv: 'PV', home: 'Home', battery: 'Battery', grid: 'Grid',
    import: 'import', export: 'export',
    soc_forecast: 'SoC forecast',
    soc_forecast_hint: 'Forecast (before: past hours)',
    price_this_hour: 'This hour price',
    plan_until: 'Plan until',
    power_unset: 'power not set',
    plan_target: 'plan target',
    source: 'source',
    purpose: 'purpose',
    export_blocked: 'export blocked',
    past_hour: 'past hour',
    from_measurement: 'from measurement',
    self_consume: 'Self-consumption', export_pv: 'PV → grid',
    charge_from_pv: 'Charge from PV', charge_from_grid: 'Charge from grid',
    discharge_self: 'Battery → home', discharge_sell: 'Battery → grid',
    idle: 'Idle', grid_import: 'Grid import',
  },
};

const jezyk = (hass) => (hass && hass.language || '').toLowerCase().startsWith('pl') ? 'pl' : 'en';
const tlumacz = (hass, klucz) => (I18N[jezyk(hass)] || I18N.en)[klucz] || klucz;

/** Kierunek slotu — do PROGNOZY SoC, nie do nazywania godziny.
 *  `znak` mówi, w którą stronę idzie energia baterii w tej godzinie. */
const AKCJE = {
  charge: { znak: 1 },
  discharge: { znak: -1 },
  self_consume: { znak: 0 },
  idle: { znak: 0 },
};

/** Kategorie wizualne — te same, których używa aplikacja Volcast.
 *
 *  Kontrakt urządzenia ma cztery kierunki, bo tyle wystarcza guardom i falownikowi.
 *  Aplikacja pokazuje OSIEM kategorii: osobno ładowanie z PV i z sieci, osobno
 *  rozładowanie na dom i na sprzedaż, osobno eksport PV i import z sieci.
 *
 *  Kolory i klucze etykiet skopiowane z `services/optimizerService.ts` (MODE_COLORS,
 *  DISPLAY_COLORS) i `i18n/locales/pl/dashboard.json` (optimizer.modes), żeby ta
 *  sama godzina nazywała się w aplikacji i na karcie tak samo. */
const KATEGORIE = {
  SELF_CONSUME: { kolor: '#00FF9D', klucz: 'self_consume' },
  EXPORT_PV: { kolor: '#F59E0B', klucz: 'export_pv' },
  CHARGE_FROM_PV: { kolor: '#34D399', klucz: 'charge_from_pv' },
  CHARGE_FROM_GRID: { kolor: '#818CF8', klucz: 'charge_from_grid' },
  BATTERY_DISCHARGE_SELF: { kolor: '#FB923C', klucz: 'discharge_self' },
  BATTERY_DISCHARGE_SELL: { kolor: '#EF4444', klucz: 'discharge_sell' },
  IDLE: { kolor: '#475569', klucz: 'idle' },
  GRID_IMPORT: { kolor: '#60A5FA', klucz: 'grid_import' },
};

/** Próg netto importu (kWh), powyżej którego tryb pasywny pokazujemy jako import.
 *  Ta sama wartość co `NET_IMPORT_DISPLAY_THRESHOLD_KWH` w aplikacji. */
const PROG_IMPORTU_KWH = 0.1;

/** Kategoria wizualna slotu — z trybu planu, a gdy go brak, z kierunku.
 *
 *  Plany utrwalone przed rozszerzeniem kontraktu nie mają `plan_mode`; wtedy
 *  odtwarzamy kategorię z kierunku i pól opisowych. Jest to przybliżenie —
 *  `EXPORT_PV` nie da się w ten sposób odróżnić od autokonsumpcji — ale plan
 *  odświeża się co pięć minut, więc dotyczy wyłącznie pierwszych chwil po
 *  aktualizacji integracji. */
const kategoria = (s) => {
  // Źródło prawdy: kategoria policzona RAZ w chmurze (z przepływów i delty SoC,
  // której karta nie ma) — ta sama, którą pokazuje aplikacja. Reguły poniżej to
  // wyłącznie fallback dla planów utrwalonych przed tym polem.
  if (s.display_kind && KATEGORIE[s.display_kind]) return s.display_kind;
  let k = s.plan_mode;
  if (!k || !KATEGORIE[k]) k = kategoriaZKierunku(s);
  // Ta sama reguła co w aplikacji: tryb PASYWNY, który netto importuje, kłamie
  // kolorem „autokonsumpcja" — bateria jest pusta i prąd realnie kupujemy.
  if ((k === 'SELF_CONSUME' || k === 'IDLE') && s.import_kwh != null) {
    const netto = s.import_kwh - (s.export_kwh || 0);
    if (netto > PROG_IMPORTU_KWH) return 'GRID_IMPORT';
  }
  return k;
};

const kategoriaZKierunku = (s) => {
  if (s.action === 'charge') {
    return s.charge_source === 'pv' ? 'CHARGE_FROM_PV' : 'CHARGE_FROM_GRID';
  }
  if (s.action === 'discharge') {
    return s.discharge_purpose === 'sell'
      ? 'BATTERY_DISCHARGE_SELL' : 'BATTERY_DISCHARGE_SELF';
  }
  return s.action === 'idle' ? 'IDLE' : 'SELF_CONSUME';
};

const opisKategorii = (s) => KATEGORIE[kategoria(s)] || KATEGORIE.SELF_CONSUME;

/** Etykieta pigułki sterowania.
 *
 * `a.control_active` to wynik ŁĄCZONY (zgoda konta ORAZ lokalny przełącznik) —
 * sam w sobie nie mówi WIDZOWI, KTÓRY z dwóch warunków go zablokował. Zgadywanie
 * powodu z samej zgody konta myliłoby dwa realne przypadki:
 *   1. Przełącznik lokalny WYŁĄCZONY (większość instalacji: domyślny stan) —
 *      to ZAWSZE decyzja właściciela, niezależnie od tego, co mówi konto.
 *      Sprawdzamy to PRZED zgodą, właśnie żeby stan konta nie mógł tu wejść.
 *   2. Przełącznik WŁĄCZONY, ale zgoda konta NIEZNANA (`null` — integracja
 *      nigdy nie rozmawiała z chmurą: brak internetu, świeża instalacja,
 *      nieudane pobranie planu) — to NIE jest odmowa konta i nie wolno tak
 *      tego nazwać.
 * Dwa dodatkowe stany idą z powodu ostatniej decyzji (`a.reason`) i mają
 * pierwszeństwo przed powyższym: `paused` — sterowanie wstrzymało się samo,
 * bo ktoś inny zmienił nastawy falownika; `unverified_profile` — sterowanie
 * liczy plan, ale jeszcze nic nie zapisuje (tryb próbny). */
function etykietaSterowania(a, hass) {
  const t = (k) => tlumacz(hass, k);
  if (a.reason === 'paused') return t('control_paused_foreign');
  if (a.reason === 'unverified_profile') return t('control_trial');
  if (a.control_active === true) return t('control_on');
  if (a.local_switch !== true) return t('control_off');
  if (a.account_consent === false) return t('control_paused_account');
  return t('control_no_cloud');
}

const KOL_W = 22;   // szerokość kolumny godzinowej w jednostkach viewBox
const WYS_SLUP = 96;
const WYS_SOC = 58;
const MARGINES = 6;   // dolny oddech pod slupkami; podpisy godzin sa juz w HTML

const esc = (s) => String(s).replace(/[&<>"]/g, (z) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[z]));

const godzina = (iso) => new Date(iso).getHours();

/** Moc rozbita na liczbę i jednostkę.
 *
 *  Waty do 1 kW, wyżej kilowaty — „3,2 kW" niesie tę samą informację co „3226 W",
 *  a mieści się w kafelku. Jednostka wraca OSOBNO, bo składana w jeden ciąg
 *  („-3,24 kW" w stopniu pisma liczby) wychodziła poza kafelek przy pięciu
 *  kolumnach: sam znak minus i spacja przed jednostką kosztują tyle, co cyfra. */
const mocRozbita = (w) => {
  if (w == null || Number.isNaN(w)) return { liczba: '—', jedn: '' };
  const abs = Math.abs(w);
  if (abs < 1000) return { liczba: String(Math.round(w)), jedn: 'W' };
  return {
    liczba: (w / 1000).toFixed(abs < 10000 ? 2 : 1).replace('.', ','),
    jedn: 'kW',
  };
};

/** Moc jednym ciągiem — do podpowiedzi, gdzie miejsca nie brakuje. */
const moc = (w) => {
  const { liczba: l, jedn } = mocRozbita(w);
  return jedn ? l + ' ' + jedn : l;
};

class VolcastPlanCard extends HTMLElement {
  static getStubConfig() {
    return { entity: 'sensor.volcast_control_plan' };
  }

  setConfig(config) {
    // NIE rzucamy przy braku `entity`. `preview: true` w customCards każe HA zbudować
    // podgląd karty bez hass i bez konfiguracji — wyjątek w tym miejscu HA pokazuje
    // jako "configuration error" NA DASHBOARDZIE, nie tylko w podglądzie.
    // Brak encji to stan do pokazania użytkownikowi, a nie powód do wysadzenia karty.
    this._config = Object.assign({ entity: 'sensor.volcast_control_plan' }, config || {});
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    this._render();
  }

  getCardSize() { return 8; }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  /** Wartość liczbowa encji falownika albo null. Zawsze lokalnie, nigdy z chmury. */
  _val(id) {
    if (!id || !this._hass || !this._hass.states) return null;
    const st = this._hass.states[id];
    if (!st || st.state === 'unknown' || st.state === 'unavailable') return null;
    const v = Number(st.state);
    return Number.isNaN(v) ? null : v;
  }

  _t(klucz) {
    return tlumacz(this._hass, klucz);
  }

  _render() {
    if (!this.shadowRoot || !this._config) return;
    const st = this._hass && this._hass.states
      ? this._hass.states[this._config.entity]
      : null;
    if (!st) {
      this.shadowRoot.innerHTML = this._szkielet(
        '<div class="pusto">' + this._t('no_entity') + ' <code>'
        + esc(this._config.entity) + '</code></div>');
      return;
    }
    const a = st.attributes || {};
    const slots = Array.isArray(a.slots) ? a.slots : [];
    const enc = a.entities || {};
    const soc = this._val(enc.soc);

    this.shadowRoot.innerHTML = this._szkielet(
      this._naglowek(st, a)
      + this._teraz(enc, soc)
      + (slots.length
        ? this._wykres(slots, soc, Number(a.battery_capacity_kwh) || 10)
        : '<div class="pusto">' + this._t('no_plan') + '</div>')
      + (slots.length ? this._legenda(slots) : '')
      + this._stopka(a));
  }

  _naglowek(st, a) {
    const slots = Array.isArray(a.slots) ? a.slots : [];
    const biezacy = slots.find((s) => s.now);
    // Nazwa bierze sie z KATEGORII biezacej godziny, nie z samego kierunku —
    // inaczej naglowek mowilby „Rozladowanie" w godzinie, w ktorej plan przewiduje
    // zwykle pokrycie domu z baterii.
    const cfg = biezacy
      ? opisKategorii(biezacy)
      : (KATEGORIE[kategoriaZKierunku(
          { action: String(st.state || '').replace(' (fallback)', '') })]
         || KATEGORIE.SELF_CONSUME);
    const sterowanie = a.control_active === true;
    const etykietaPigulki = etykietaSterowania(a, this._hass);
    return ''
      + '<div class="head">'
      + '<div class="teraz-tryb">'
      + '<span class="kropka" style="--k:' + cfg.kolor + '"></span>'
      + '<span class="tytul">' + this._t(cfg.klucz) + (a.fallback ? ' · fallback' : '') + '</span>'
      + '</div>'
      + '<span class="pigulka ' + (sterowanie ? 'on' : 'off') + '">'
      + esc(etykietaPigulki)
      + '</span>'
      + '</div>';
  }

  /** Pas wartości bieżących — wszystko z encji falownika, na żywo. */
  _teraz(enc, soc) {
    const pv = this._val(enc.pv);
    const dom = this._val(enc.load);
    const bat = this._val(enc.battery);
    const siec = this._val(enc.grid);
    const kafel = (etykieta, wartosc, jedn, kolor, dopisek) => ''
      + '<div class="kafel">'
      + '<span class="et">' + etykieta + '</span>'
      + '<b style="color:' + kolor + '">' + wartosc
      + (jedn ? '<i>' + jedn + '</i>' : '') + '</b>'
      + (dopisek ? '<span class="dop">' + dopisek + '</span>' : '')
      + '</div>';
    const kafelMocy = (etykieta, wartosc, kolor, dopisek) => {
      const m = mocRozbita(wartosc);
      return kafel(etykieta, m.liczba, m.jedn, kolor, dopisek);
    };
    return '<div class="teraz">'
      + kafel(this._t('soc'), soc == null ? '—' : String(Math.round(soc)), soc == null ? '' : '%',
        PALETA.primaryBright, soc != null ? this._pasekSoc(soc) : '')
      + kafelMocy(this._t('pv'), pv, PALETA.amber)
      + kafelMocy(this._t('home'), dom, PALETA.violet)
      + kafelMocy(this._t('battery'), bat, bat != null && bat < 0 ? PALETA.primary : PALETA.orange)
      + kafelMocy(this._t('grid'), siec, siec != null && siec > 0 ? PALETA.danger : PALETA.sky,
        siec == null ? '' : (siec > 0 ? this._t('import') : this._t('export')))
      + '</div>';
  }

  _pasekSoc(soc) {
    return '<span class="soc-bar"><i style="width:'
      + Math.max(0, Math.min(100, soc)) + '%"></i></span>';
  }

  /**
   * Kolumny godzinowe + krzywa prognozy SoC w jednym SVG.
   *
   * Prognoza: start = REALNY SoC z falownika, potem per slot
   * delta = moc_bateryjna * 1 h / pojemność. `power_w` jest po stronie baterii,
   * więc to jest dokładnie ta wielkość, która zmienia SoC.
   */
  /** Prognoza SoC godzina po godzinie: ile jest na początku, ile na końcu.
   *
   *  Liczona RAZ i dzielona przez krzywą i podpowiedzi. Gdyby każde z nich liczyło
   *  osobno, prędzej czy później rozjechałyby się o zaokrąglenie albo o jedną
   *  godzinę — a wtedy karta pokazywałaby w dymku co innego niż na linii.
   *
   *  Historycznego SoC nie mamy: siedzi w recorderze HA, nie w planie. Godziny
   *  minione dostają `null`, zamiast zmyślonej wartości.
   *
   *  Bieżąca godzina zaczyna się od POMIARU, nie od projekcji — i to jest cała
   *  wartość tej krzywej: przy każdym odświeżeniu sama się koryguje, zamiast
   *  odjeżdżać od rzeczywistości razem z planem sprzed doby.
   */
  _prognozaSoc(slots, iTeraz, socTeraz, pojemnosc) {
    const puste = slots.map(() => null);
    if (socTeraz == null || !(pojemnosc > 0)) return puste;

    const out = puste.slice();
    let soc = socTeraz;
    for (let i = iTeraz; i < slots.length; i += 1) {
      // Znak z KIERUNKU, nie z kategorii: to fizyka baterii, a nie nazwa dla człowieka.
      const kier = AKCJE[slots[i].action] || AKCJE.idle;
      const kwh = ((slots[i].power_w || 0) / 1000) * kier.znak;
      const koniec = Math.max(0, Math.min(100, soc + (kwh / pojemnosc) * 100));
      out[i] = { od: soc, do: koniec };
      soc = koniec;
    }
    return out;
  }

  _wykres(slots, socTeraz, pojemnosc) {
    const n = slots.length;
    const W = n * KOL_W;
    const H = WYS_SLUP + WYS_SOC + MARGINES;
    const dolSlupka = WYS_SOC + WYS_SLUP;
    const wysPx = Math.round(H * 1.35);
    // Ten sam przelicznik, którym pozycjonujemy oś w HTML. SVG skaluje się
    // nierównomiernie, ale W PIONIE jest to zwykłe mnożenie — więc etykieta
    // wyliczona tak siada dokładnie na swojej linii.
    const skala = wysPx / H;
    const maks = Math.max.apply(null,
      slots.map((s) => Math.abs(s.power_w || 0)).concat([1]));

    // Indeks bieżącej godziny. Wszystko przed nim JUŻ SIĘ WYDARZYŁO — plan na te
    // godziny jest historią, a nie zapowiedzią, więc nie może wyglądać tak samo.
    let iTeraz = slots.findIndex((s) => s.now);
    if (iTeraz < 0) iTeraz = 0;

    // Skala pierwiastkowa. Liniowa gubiła wszystko poniżej ~1 kW przy jednej
    // godzinie sprzedaży 4–5 kW: słupki 200 W miały kilka pikseli i nie dało się
    // w nie trafić kursorem. Pierwiastek zachowuje kolejność, a spłaszcza górę.
    const wysokosc = (p) => Math.max(4,
      Math.round(Math.sqrt(Math.abs(p || 0) / maks) * (WYS_SLUP - 8)));

    const yDlaSoc = (v) => WYS_SOC - (v / 100) * (WYS_SOC - 8) - 4;
    const prog = this._prognozaSoc(slots, iTeraz, socTeraz, pojemnosc);
    const maSoc = prog.some(Boolean);

    let kolumny = '';
    let siatka = '';
    let obszary = '';

    // Poziomy odniesienia SoC. Bez nich linia mówiła tylko „rośnie / spada" —
    // nie dało się odczytać, czy wieczorem zostaje 20 %, czy 60 %.
    if (maSoc) {
      [100, 50, 0].forEach((v) => {
        siatka += '<line x1="0" y1="' + yDlaSoc(v).toFixed(1) + '" x2="' + W
          + '" y2="' + yDlaSoc(v).toFixed(1) + '" stroke="rgba(255,255,255,'
          + (v === 50 ? '.05' : '.09') + ')" stroke-width="1"'
          + (v === 50 ? ' stroke-dasharray="3 3"' : '')
          + ' vector-effect="non-scaling-stroke"/>';
      });
    }

    const t = (k) => this._t(k);

    slots.forEach((s, i) => {
      const cfg = opisKategorii(s);
      const h = wysokosc(s.power_w);
      const x = i * KOL_W;
      const y = dolSlupka - h;
      const przeszlosc = i < iTeraz;
      const p = prog[i];
      const tytul = [
        String(godzina(s.from)).padStart(2, '0') + ':00–'
          + String(godzina(s.to)).padStart(2, '0') + ':00',
        t(cfg.klucz),
        s.power_w != null ? moc(s.power_w) : t('power_unset'),
        s.price != null ? Number(s.price).toFixed(2).replace('.', ',') + ' zł/kWh' : null,
        // Prognoza SoC na tę godzinę — to, po co ta krzywa w ogóle jest.
        // Bieżąca godzina startuje od pomiaru, nie od projekcji; mówimy to wprost,
        // bo inaczej wartość wyglądałaby na wyliczoną dla pełnej godziny.
        p ? 'SoC ' + Math.round(p.od) + ' → ' + Math.round(p.do) + ' %'
          + (i === iTeraz ? ' (' + t('from_measurement') + ')' : '') : null,
        s.soc_target != null ? t('plan_target') + ' ' + Math.round(s.soc_target) + ' %' : null,
        s.charge_source ? t('source') + ': ' + s.charge_source : null,
        s.discharge_purpose ? t('purpose') + ': ' + s.discharge_purpose : null,
        s.export_allowed === false ? t('export_blocked') : null,
        przeszlosc ? t('past_hour') : null,
      ].filter(Boolean).join(' · ');

      if (s.now) {
        siatka += '<rect x="' + x + '" y="0" width="' + KOL_W + '" height="'
          + dolSlupka + '" fill="rgba(255,255,255,.07)" rx="3"/>';
      }

      kolumny += '<rect x="' + (x + 2) + '" y="' + y + '" width="' + (KOL_W - 4)
        + '" height="' + h + '" rx="3" fill="' + cfg.kolor + '" opacity="'
        + (s.power_w == null ? '.28' : (przeszlosc ? '.4' : '1')) + '"/>';

      // CAŁA kolumna jest obszarem najechania, nie sam słupek. Przy niskich mocach
      // słupek ma kilka pikseli i trafienie w niego było loterią.
      obszary += '<g class="hit"><title>' + esc(tytul) + '</title>'
        + '<rect x="' + x + '" y="0" width="' + KOL_W + '" height="' + dolSlupka
        + '" fill="transparent"/></g>';

      if (godzina(s.from) % 3 === 0) {
        siatka += '<line x1="' + x + '" y1="0" x2="' + x + '" y2="' + dolSlupka
          + '" stroke="rgba(255,255,255,.07)" stroke-width="1"'
          + ' vector-effect="non-scaling-stroke"/>';
      }
    });

    // Podpisy godzin w HTML, NIE w SVG. Wykres skaluje się nierównomiernie
    // (`preserveAspectRatio="none"`), żeby słupki wypełniały całą szerokość karty
    // niezależnie od liczby slotów — a to rozciąga tekst w pionie tym mocniej, im
    // węższa karta. Siatka HTML o tej samej liczbie kolumn trzyma podpisy dokładnie
    // pod słupkami i nie deformuje pisma.
    const podpisy = '<div class="godziny" style="grid-template-columns:repeat('
      + n + ',1fr)">'
      + slots.map((s) => {
        const g = godzina(s.from);
        return '<span>' + (g % 3 === 0 ? String(g).padStart(2, '0') : '') + '</span>';
      }).join('')
      + '</div>';

    // Etykiety osi też w HTML i z tego samego powodu co godziny.
    const os = maSoc
      ? '<div class="os-soc" style="height:' + wysPx + 'px">'
        + [100, 50, 0].map((v) => '<span style="top:'
          + (yDlaSoc(v) * skala).toFixed(1) + 'px">' + v + '%</span>').join('')
        + '</div>'
      : '<div class="os-soc" style="height:' + wysPx + 'px"></div>';

    // Obszar prognozy: od bieżącej godziny w prawo, w ukośne kreski — jak w aplikacji.
    // Bez tego nie widać, gdzie kończy się fakt, a zaczyna założenie.
    const xProg = iTeraz * KOL_W;
    const pasProgozy = '<rect x="' + xProg + '" y="0" width="' + (W - xProg)
      + '" height="' + dolSlupka + '" fill="url(#volcast-kreski)"/>';

    // Krzywa SoC WYŁĄCZNIE nad prognozą — historycznego SoC nie mamy, więc linia
    // nad przeszłością byłaby zmyślaniem danych, a nie informacją.
    let krzywa = '';
    let punktStart = '';
    if (maSoc) {
      const punkty = [(xProg + 1).toFixed(1) + ',' + yDlaSoc(socTeraz).toFixed(1)];
      for (let i = iTeraz; i < n; i += 1) {
        if (!prog[i]) continue;
        punkty.push((i * KOL_W + KOL_W).toFixed(1) + ','
          + yDlaSoc(prog[i].do).toFixed(1));
      }
      // `non-scaling-stroke`: bez tego niejednorodne skalowanie robi z linii
      // wstęgę — grubszą w pionie, cieńszą w poziomie.
      krzywa = '<polyline points="' + punkty.join(' ') + '" fill="none" stroke="'
        + PALETA.primaryBright + '" stroke-width="2" stroke-linejoin="round"'
        + ' stroke-linecap="round" vector-effect="non-scaling-stroke"/>';
      punktStart = '<circle cx="' + (xProg + 1) + '" cy="' + yDlaSoc(socTeraz)
        + '" r="2.6" fill="' + PALETA.primaryBright + '"/>';
    }

    const defs = '<defs><pattern id="volcast-kreski" width="6" height="6" '
      + 'patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
      + '<line x1="0" y1="0" x2="0" y2="6" stroke="rgba(255,255,255,.06)" '
      + 'stroke-width="2"/></pattern></defs>';

    return '<div class="wykres">' + os + '<div class="plotno">'
      + '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" '
      + 'style="width:100%;height:' + wysPx + 'px">'
      + defs + pasProgozy + siatka + krzywa + punktStart + kolumny + obszary
      + '</svg>' + podpisy + '</div></div>';
  }

  _legenda(slots) {
    const obecne = slots.map(kategoria).filter((v, i, t) => t.indexOf(v) === i);
    return '<div class="legenda">' + obecne.map((k) => {
      const cfg = KATEGORIE[k] || KATEGORIE.SELF_CONSUME;
      return '<span class="poz"><i style="--k:' + cfg.kolor + '"></i>' + this._t(cfg.klucz) + '</span>';
    }).join('')
      + '<span class="poz"><i class="linia"></i>' + this._t('soc_forecast') + '</span>'
      + '<span class="poz"><i class="kreski"></i>' + this._t('soc_forecast_hint') + '</span></div>';
  }

  _stopka(a) {
    const lang = jezyk(this._hass);
    const doKiedy = a.valid_until
      ? new Date(a.valid_until).toLocaleString(lang === 'pl' ? 'pl-PL' : 'en-GB',
        { weekday: 'short', hour: '2-digit', minute: '2-digit' })
      : '—';
    const cena = a.price != null ? Number(a.price).toFixed(2) + ' zł/kWh' : '—';
    return '<div class="stopka">'
      + '<span>' + this._t('price_this_hour') + ' <b>' + cena + '</b></span>'
      + '<span>' + this._t('plan_until') + ' <b>' + doKiedy + '</b></span>'
      + '</div>';
  }

  _szkielet(tresc) {
    return ''
      + '<style>'
      + ':host{display:block}'
      + '.karta{background:linear-gradient(160deg,' + PALETA.surface + ' 0%,' + PALETA.canvas + ' 100%);'
      + 'border:1px solid ' + PALETA.border + ';border-radius:24px;padding:18px 20px;'
      + 'color:' + PALETA.textPrimary + ';'
      + 'font-family:Outfit,"DM Sans",Roboto,system-ui,-apple-system,sans-serif;'
      + 'box-shadow:0 10px 30px rgba(0,0,0,.35)}'
      // `flex-wrap`: przy bardzo waskiej karcie pigulka schodzi pod tytul, zamiast
      // sciskac go do „Lad...". Nazwa biezacego trybu jest wazniejsza od tego, zeby
      // naglowek zmiescil sie w jednej linii.
      + '.head{display:flex;flex-wrap:wrap;justify-content:space-between;'
      + 'align-items:center;gap:8px 12px}'
      + '.teraz-tryb{display:flex;align-items:center;gap:10px;min-width:0}'
      + '.kropka{width:11px;height:11px;border-radius:9999px;background:var(--k);'
      + 'box-shadow:0 0 0 4px color-mix(in srgb,var(--k) 18%,transparent);flex:0 0 auto}'
      // Tytul ustepuje pierwszy. Bez `min-width:0` i wielokropka pigulka wchodzila
      // na napis „Ladowanie" przy karcie ponizej ~300 px — flex nie zwezi elementu
      // ponizej jego tresci, dopoki mu sie tego nie pozwoli.
      + '.tytul{font-size:19px;font-weight:700;letter-spacing:-.2px;min-width:0;'
      + 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap}'
      + '.pigulka{padding:5px 11px;border-radius:9999px;font-size:11px;white-space:nowrap;'
      + 'flex:0 0 auto;border:1px solid ' + PALETA.borderMid + '}'
      + '.pigulka.on{color:' + PALETA.primaryBright + ';background:rgba(52,211,153,.10);'
      + 'border-color:rgba(52,211,153,.35)}'
      + '.pigulka.off{color:' + PALETA.textSecondary + ';background:rgba(255,255,255,.03)}'
      // `auto-fit` zamiast sztywnych piatki i media query. Prog `@media` mierzy OKNO,
      // a karta ma wlasna szerokosc — na szerokim pulpicie z waska kolumna dashboardu
      // kafelki dalej probowaly zmiescic sie w pieciu, a w waskim oknie lamaly sie
      // nawet wtedy, gdy karta byla szeroka. `minmax` rozstrzyga to szerokoscia KARTY.
      // Prog 70 px, nie 74: przy karcie 440 px zostaje 400 px na tresc, cztery odstepy
      // po 6 px zjadaja 24, wiec na kolumne przypada 75 px. Prog 74 mijal sie o wlos
      // i lamal piatke na cztery.
      + '.teraz{display:grid;grid-template-columns:repeat(auto-fit,minmax(70px,1fr));'
      + 'gap:6px;margin:16px 0 4px}'
      + '.kafel{background:rgba(255,255,255,.03);border:1px solid ' + PALETA.border + ';'
      + 'border-radius:14px;padding:9px 8px;min-width:0}'
      + '.kafel .et{display:block;font-size:9px;letter-spacing:.5px;text-transform:uppercase;'
      + 'color:' + PALETA.textMuted + '}'
      // 15px zamiast 17px i jednostka o polowe mniejsza: przy pieciu kolumnach
      // kafelek ma okolo 78 px, a „-3,24 kW" w stopniu pisma liczby zajmowal wiecej.
      // `min-width:0` na kafelku pozwala siatce go sciesnic zamiast rozpychac karte.
      + '.kafel b{display:flex;align-items:baseline;gap:2px;font-size:14px;'
      + 'font-weight:600;margin-top:2px;font-variant-numeric:tabular-nums;'
      + 'white-space:nowrap;overflow:hidden}'
      + '.kafel b i{font-style:normal;font-size:9px;font-weight:500;letter-spacing:.3px;'
      + 'color:' + PALETA.textMuted + ';flex:0 0 auto}'
      + '.kafel .dop{font-size:10px;color:' + PALETA.textMuted + '}'
      + '.soc-bar{display:block;height:3px;border-radius:9999px;margin-top:6px;'
      + 'background:rgba(255,255,255,.08);overflow:hidden}'
      + '.soc-bar i{display:block;height:100%;background:' + PALETA.primary + '}'
      // Os po lewej, plotno po prawej. Etykiety musza byc w HTML z tego samego
      // powodu co godziny: tekst w SVG rozciaga sie razem z wykresem.
      + '.wykres{display:grid;grid-template-columns:24px 1fr;margin:10px -2px 0}'
      + '.os-soc{position:relative}'
      + '.os-soc span{position:absolute;right:5px;transform:translateY(-50%);'
      + 'font-size:8px;color:' + PALETA.textMuted + ';font-variant-numeric:tabular-nums;'
      + 'white-space:nowrap}'
      + '.plotno{min-width:0}'
      + '.godziny{display:grid;margin-top:3px}'
      // `overflow:visible`: komorka siatki ma szerokosc JEDNEJ kolumny wykresu, przy
      // waskiej karcie okolo 9 px, a „21" zajmuje wiecej. Przycinanie robilo z podpisow
      // „1ε 0( 0ς". Sasiednie komorki sa puste (podpisujemy co trzecia godzine), wiec
      // napis ma gdzie wystawac i nic nie zasloni.
      + '.godziny span{font-size:9px;text-align:center;color:' + PALETA.textMuted + ';'
      + 'font-variant-numeric:tabular-nums;overflow:visible;white-space:nowrap}'
      + 'svg g.hit rect{fill:transparent}'
      + 'svg g.hit:hover rect{fill:rgba(255,255,255,.09)}'
      + '.poz i.kreski{width:14px;height:9px;border-radius:3px;'
      + 'background:repeating-linear-gradient(45deg,rgba(255,255,255,.22) 0 2px,'
      + 'transparent 2px 5px);border:1px solid ' + PALETA.border + '}'
      + '.legenda{display:flex;flex-wrap:wrap;gap:6px 12px;margin-top:12px}'
      + '.poz{display:flex;align-items:center;gap:5px;font-size:11px;'
      + 'white-space:nowrap;color:' + PALETA.textSecondary + '}'
      + '.poz i{width:9px;height:9px;border-radius:3px;background:var(--k)}'
      + '.poz i.linia{width:14px;height:2px;border-radius:2px;background:'
      + PALETA.primaryBright + '}'
      + '.stopka{margin-top:14px;padding-top:12px;border-top:1px solid ' + PALETA.border + ';'
      + 'display:flex;flex-wrap:wrap;gap:2px 14px;justify-content:space-between;'
      + 'font-size:12px;color:' + PALETA.textSecondary + '}'
      + '.stopka b{color:' + PALETA.textPrimary + ';font-weight:600;'
      + 'font-variant-numeric:tabular-nums}'
      + '.pusto{padding:26px 0;text-align:center;color:' + PALETA.textMuted + ';font-size:14px}'
      + '.karta code{font-family:ui-monospace,monospace;font-size:12px}'
      + '</style>'
      + '<div class="karta">' + tresc + '</div>';
  }
}

customElements.define('volcast-plan-card', VolcastPlanCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: 'volcast-plan-card',
  name: 'Volcast Plan',
  description: 'Kokpit magazynu: wartości bieżące z falownika, plan z chmury i prognoza SoC.',
  preview: true,
});

class VolcastPanel extends HTMLElement {
  set hass(hass) { this._hass = hass; this._render(); }
  set panel(panel) { this._panel = panel; this._render(); }
  set narrow(_value) {}
  _render() {
    if (!this._hass || !this._panel) return;
    if (!this._card) {
      this._card = document.createElement("volcast-plan-card");
      this._card.setConfig({ entity: (this._panel.config || {}).entity });
      const wrap = document.createElement("div");
      wrap.style.cssText = "max-width:960px;margin:0 auto;padding:16px;";
      wrap.appendChild(this._card);
      this.appendChild(wrap);
    }
    this._card.hass = this._hass;
  }
}
if (!customElements.get("volcast-panel")) customElements.define("volcast-panel", VolcastPanel);
