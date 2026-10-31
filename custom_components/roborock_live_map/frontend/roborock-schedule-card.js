/**
 * Roborock schedule card — create, edit, switch on/off and delete cleaning schedules.
 *
 * The robot's own schedules cannot be managed locally, so schedules live in the
 * roborock_live_map integration (run by Home Assistant) and this card talks to it over
 * the WebSocket API:
 *
 *   roborock_live_map/schedules/list    {entity_id}
 *   roborock_live_map/schedules/save    {entity_id, schedule:{id?, name, time:"HH:MM",
 *                                        days:[0..6] (Monday = 0), rooms:[ids] (empty = whole
 *                                        home), repeat, enabled}}
 *   roborock_live_map/schedules/delete  {entity_id, schedule_id}
 *
 * Listing is open to every user; save/delete need a Home Assistant administrator, so a
 * non-admin sees the schedules read-only. Room names and order come from the room-order
 * sensor's `rooms` ([{id,name}]) and `order` attributes, the same source as the rooms card.
 *
 * Config: entity (the vacuum; used as the schedule owner), order_entity (room order sensor).
 *
 * An in-progress edit is a draft that survives Home Assistant state updates, list
 * refreshes and the card being detached/re-attached (kept in sessionStorage).
 */

import { LUCENT_TOKENS, LUCENT_CONTROLS } from "./lucent-ha.js";

const WS_LIST = "roborock_live_map/schedules/list";
const WS_SAVE = "roborock_live_map/schedules/save";
const WS_DELETE = "roborock_live_map/schedules/delete";
const WS_SET_ENABLED = "roborock_live_map/schedules/set_enabled";

const POLL_MS = 60000;
const REPEATS = [1, 2, 3];
const TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/;
const WEEKDAYS = [0, 1, 2, 3, 4, 5, 6];
const WEEKDAYS_ONLY = [0, 1, 2, 3, 4];
const WEEKEND = [5, 6];

const escapeHtml = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const sameSet = (a, b) => a.length === b.length && a.every((v) => b.includes(v));

function errorMessage(err) {
  if (!err) return "Something went wrong.";
  if (err.code === "unauthorized" || err.code === "forbidden") return "Only a Home Assistant administrator can change schedules.";
  if (err.code === "unknown_command") return "This Home Assistant has no schedule support. Update the Roborock Live Map integration.";
  return err.message || String(err);
}

function normalise(raw) {
  const days = Array.isArray(raw?.days)
    ? [...new Set(raw.days.map(Number).filter((d) => d >= 0 && d <= 6))].sort((a, b) => a - b)
    : [];
  const rooms = Array.isArray(raw?.rooms) ? [...new Set(raw.rooms.map(Number).filter(Number.isFinite))] : [];
  const repeat = Number(raw?.repeat);
  return {
    id: raw?.id != null ? String(raw.id) : undefined,
    name: String(raw?.name ?? ""),
    time: typeof raw?.time === "string" ? raw.time.slice(0, 5) : "08:00",
    days,
    rooms,
    repeat: Number.isFinite(repeat) && repeat >= 1 ? Math.round(repeat) : 1,
    enabled: raw?.enabled !== false,
  };
}

class RoborockScheduleCard extends HTMLElement {
  static getStubConfig() {
    return { entity: "vacuum.dining_room_kevin", order_entity: "sensor.dining_room_kevin_room_order" };
  }

  setConfig(config) {
    if (!config?.entity) throw new Error("roborock-schedule-card: 'entity' (the vacuum) is required");
    if (!config?.order_entity) throw new Error("roborock-schedule-card: 'order_entity' (the room order sensor) is required");
    const key = `roborock-schedule-card:${config.entity}:draft`;
    if (this._draftKey && this._draftKey !== key) {
      // Entity changed: the previous entity's draft must not leak into this one.
      try { sessionStorage.removeItem(this._draftKey); } catch (_) {}
      this._draft = null;
      this._confirmId = null;
      this._rowError = {};
      this._busy = new Set();
      this._loadError = null;
      this._reqSeq++;
    }
    this._config = { ...config };
    this._draftKey = key;
    this._schedules = null;
    this._loadedFor = null;
    if (!this._draft) this._restoreDraft();
    if (this._built) this._renderAll();
  }

  getCardSize() {
    return 5;
  }

  getGridOptions() {
    return { columns: "full", min_columns: 6, min_rows: 4 };
  }

  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._built = false;
    this._schedules = null;   // null = not loaded yet
    this._loading = false;
    this._loadError = null;
    this._draft = null;       // { schedule, isNew, error, saving }
    this._confirmId = null;
    this._busy = new Set();   // ids with a toggle/delete in flight
    this._rowError = {};      // id -> message
    this._reqSeq = 0;
    this._roomsSig = null;
  }

  /* ---------------- lifecycle ---------------- */

  connectedCallback() {
    if (!this._built) this._build();
    this._pollTimer = setInterval(() => {
      if (!document.hidden && !this._draft && !this._busy.size) this._load(true);
    }, POLL_MS);
    this._visHandler = () => {
      if (!document.hidden && this._hass && !this._draft) this._load(true);
    };
    document.addEventListener("visibilitychange", this._visHandler);
  }

  disconnectedCallback() {
    clearInterval(this._pollTimer);
    document.removeEventListener("visibilitychange", this._visHandler);
    this._persistDraft();
  }

  set hass(hass) {
    const wasAdmin = this._isAdmin;
    this._hass = hass;
    if (!this._built) this._build();
    if (this._loadedFor !== this._config?.entity && this._config) this._load();
    if (wasAdmin !== this._isAdmin) this._renderAll();
    this._syncRooms();
  }

  get _isAdmin() {
    return this._hass?.user?.is_admin === true;
  }

  get _locale() {
    return this._hass?.locale?.language || this._hass?.language || undefined;
  }

  /* ---------------- data ---------------- */

  _rooms() {
    const attrs = this._hass?.states?.[this._config?.order_entity]?.attributes;
    const rooms = Array.isArray(attrs?.rooms) ? attrs.rooms : [];
    const list = rooms.map((r) => ({ id: Number(r.id), name: r.name || `Room ${r.id}` }));
    const order = Array.isArray(attrs?.order) ? attrs.order.map(Number) : [];
    const rank = (id) => {
      const i = order.indexOf(id);
      return i < 0 ? order.length : i;
    };
    return list
      .map((r, i) => ({ r, i }))
      .sort((a, b) => rank(a.r.id) - rank(b.r.id) || a.i - b.i)
      .map(({ r }) => r);
  }

  _roomName(id) {
    return this._rooms().find((r) => r.id === id)?.name ?? `Room ${id}`;
  }

  _syncRooms() {
    const sig = JSON.stringify(this._rooms());
    if (sig === this._roomsSig) return;
    this._roomsSig = sig;
    if (this._schedules) this._renderList();
    if (this._draft) this._renderRoomPicker();
  }

  async _load(quiet = false) {
    if (!this._hass || !this._config) return;
    const seq = ++this._reqSeq;
    const entity = this._config.entity;
    this._loadedFor = entity;
    if (!quiet || !this._schedules) {
      this._loading = true;
      this._renderList();
    }
    try {
      const res = await this._hass.callWS({ type: WS_LIST, entity_id: entity });
      if (seq !== this._reqSeq) return;
      this._schedules = (Array.isArray(res) ? res : []).map(normalise);
      this._loadError = null;
    } catch (err) {
      if (seq !== this._reqSeq) return;
      if (!quiet || !this._schedules) this._loadError = errorMessage(err);
    }
    this._loading = false;
    this._renderList();
  }

  async _callSave(schedule) {
    const payload = { name: schedule.name, time: schedule.time, days: schedule.days, rooms: schedule.rooms, repeat: schedule.rooms.length ? schedule.repeat : 1, enabled: schedule.enabled };
    if (schedule.id) payload.id = schedule.id;
    const saved = await this._hass.callWS({ type: WS_SAVE, entity_id: this._config.entity, schedule: payload });
    return normalise(saved);
  }

  _upsert(saved) {
    const list = this._schedules ? [...this._schedules] : [];
    const i = list.findIndex((s) => s.id === saved.id);
    if (i >= 0) list[i] = saved;
    else list.push(saved);
    this._schedules = list;
  }

  async _toggle(id) {
    const cur = this._schedules?.find((s) => s.id === id);
    if (!cur || this._busy.has(id) || !this._isAdmin) return;
    this._busy.add(id);
    delete this._rowError[id];
    this._renderList();
    try {
      const res = await this._hass.callWS({ type: WS_SET_ENABLED, entity_id: this._config.entity, schedule_id: id, enabled: !cur.enabled });
      const saved = normalise(res);
      this._upsert(saved);
      this._announce(`${saved.name || "Schedule"} ${saved.enabled ? "enabled" : "disabled"}.`);
    } catch (err) {
      this._rowError[id] = errorMessage(err);
    }
    this._busy.delete(id);
    this._renderList();
  }

  async _delete(id) {
    if (this._busy.has(id) || !this._isAdmin) return;
    this._busy.add(id);
    delete this._rowError[id];
    this._renderList();
    try {
      await this._hass.callWS({ type: WS_DELETE, entity_id: this._config.entity, schedule_id: id });
      this._schedules = (this._schedules || []).filter((s) => s.id !== id);
      this._confirmId = null;
      if (this._draft?.schedule.id === id) this._closeEditor();
    } catch (err) {
      this._rowError[id] = errorMessage(err);
      this._busy.delete(id);
      this._renderList();
      this.shadowRoot.querySelector(`[data-error-for="${CSS.escape(id)}"]`)?.focus();
      return;
    }
    this._busy.delete(id);
    this._renderList();
    this._announce("Schedule deleted.");
    this._focusAfter = "add";
    this._restoreFocus();
  }

  /* ---------------- draft ---------------- */

  _persistDraft() {
    try {
      if (this._draft) sessionStorage.setItem(this._draftKey, JSON.stringify({ schedule: this._draft.schedule, isNew: this._draft.isNew, scope: this._draft.scope }));
      else sessionStorage.removeItem(this._draftKey);
    } catch (_) {}
  }

  _restoreDraft() {
    try {
      const raw = sessionStorage.getItem(this._draftKey);
      if (!raw) return;
      const { schedule, isNew, scope } = JSON.parse(raw);
      const norm = normalise(schedule);
      this._draft = { schedule: norm, isNew: !!isNew, scope: scope === "rooms" || scope === "home" ? scope : norm.rooms.length ? "rooms" : "home", error: null, saving: false };
    } catch (_) {}
  }

  _openEditor(id) {
    const existing = id ? this._schedules?.find((s) => s.id === id) : null;
    const schedule = existing
      ? { ...existing, days: [...existing.days], rooms: [...existing.rooms] }
      : { id: undefined, name: "", time: "08:00", days: [...WEEKDAYS_ONLY], rooms: [], repeat: 1, enabled: true };
    this._draft = { schedule, isNew: !existing, scope: schedule.rooms.length ? "rooms" : "home", error: null, saving: false };
    this._confirmId = null;
    this._persistDraft();
    this._renderAll();
    this.shadowRoot.querySelector("#f-name")?.focus();
  }

  _closeEditor() {
    this._draft = null;
    this._persistDraft();
    this._renderAll();
  }

  _validate(s, scope) {
    const errors = {};
    if (!s.name.trim()) errors.name = "Give the schedule a name.";
    if (!TIME_RE.test(s.time)) errors.time = "Pick a time.";
    if (!s.days.length) errors.days = "Pick at least one day.";
    if (scope === "rooms" && !s.rooms.length) errors.rooms = "Choose at least one room, or switch to Whole home.";
    return errors;
  }

  async _saveDraft() {
    const d = this._draft;
    if (!d || d.saving || !this._isAdmin) return;
    d.schedule.name = d.schedule.name.trim();
    d.errors = this._validate(d.schedule, d.scope);
    d.error = null;
    if (Object.keys(d.errors).length) {
      this._renderEditor();
      const first = { name: "#f-name", time: "#f-time", days: "#days-group button", rooms: "#rooms [data-act=scope][data-scope=rooms]" }[Object.keys(d.errors)[0]];
      this.shadowRoot.querySelector(first)?.focus();
      return;
    }
    d.saving = true;
    this._renderEditor();
    try {
      const saved = await this._callSave({ ...d.schedule, rooms: d.scope === "home" ? [] : d.schedule.rooms });
      this._upsert(saved);
      if (this._draft === d) {
        this._draft = null;
        this._persistDraft();
        this._focusAfter = "add";
      }
    } catch (err) {
      d.saving = false;
      d.error = errorMessage(err);
      this._renderAll();
      return;
    }
    this._renderAll();
    this._restoreFocus();
  }

  _announce(msg) {
    const el = this.shadowRoot.querySelector("#announce");
    if (el) el.textContent = msg;
  }

  _restoreFocus() {
    if (this._focusAfter === "add") this.shadowRoot.querySelector("#add")?.focus();
    this._focusAfter = null;
  }

  /* ---------------- formatting ---------------- */

  _dayName(d, style) {
    // 2024-01-01 is a Monday, so index == Monday-based day number.
    return new Date(2024, 0, 1 + d).toLocaleDateString(this._locale, { weekday: style });
  }

  _time(t) {
    if (!TIME_RE.test(t)) return t;
    const [h, m] = t.split(":").map(Number);
    return new Date(2024, 0, 1, h, m).toLocaleTimeString(this._locale, { hour: "numeric", minute: "2-digit" });
  }

  _daysText(days) {
    if (sameSet(days, WEEKDAYS)) return "Every day";
    if (sameSet(days, WEEKDAYS_ONLY)) return "Weekdays";
    if (sameSet(days, WEEKEND)) return "Weekends";
    if (!days.length) return "No days";
    return days.map((d) => this._dayName(d, "short")).join(", ");
  }

  _roomsText(rooms) {
    if (!rooms.length) return "Whole home";
    return rooms.map((id) => this._roomName(id)).join(", ");
  }

  /* ---------------- rendering ---------------- */

  _build() {
    this._built = true;
    this.shadowRoot.innerHTML = `<style>${STYLES}</style>
<ha-card>
  <div class="head">
    <div class="title"><ha-icon icon="mdi:calendar-clock"></ha-icon><span>Schedules</span></div>
    <div class="head-actions">
      <button class="icon-button" id="refresh" type="button" aria-label="Refresh schedules"><ha-icon icon="mdi:refresh"></ha-icon></button>
      <button class="pill primary" id="add" type="button" aria-label="Add schedule"><ha-icon icon="mdi:plus"></ha-icon><span>Add</span></button>
    </div>
  </div>
  <div id="banner" class="banner"></div>
  <div id="announce" class="sr-only" role="status" aria-live="polite"></div>
  <div id="list" class="body"></div>
  <form id="editor" class="body editor" novalidate hidden></form>
</ha-card>`;
    const root = this.shadowRoot;
    root.addEventListener("click", (e) => this._onClick(e));
    root.addEventListener("input", (e) => this._onInput(e));
    root.addEventListener("change", (e) => this._onInput(e));
    root.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && this._confirmId) {
        const id = this._confirmId;
        this._confirmId = null;
        this._renderList();
        root.querySelector(`[data-act="ask-delete"][data-id="${CSS.escape(id)}"]`)?.focus();
      }
    });
    root.querySelector("#editor").addEventListener("submit", (e) => {
      e.preventDefault();
      this._saveDraft();
    });
    this._renderAll();
  }

  _renderAll() {
    if (!this._built) return;
    const root = this.shadowRoot;
    const editing = !!this._draft;
    root.querySelector("#list").hidden = editing;
    root.querySelector("#editor").hidden = !editing;
    const add = root.querySelector("#add");
    add.hidden = editing || !this._isAdmin;
    root.querySelector("#refresh").hidden = editing;
    const banner = root.querySelector("#banner");
    const bannerText = this._isAdmin ? "" : "View only. A Home Assistant administrator is needed to add, change or delete schedules.";
    if (banner.textContent !== bannerText) banner.textContent = bannerText;
    banner.hidden = this._isAdmin;
    this._renderList();
    if (editing) this._renderEditor();
  }

  _renderList() {
    if (!this._built) return;
    const el = this.shadowRoot.querySelector("#list");
    const admin = this._isAdmin;
    let html = "";
    if (this._loadError && !this._schedules) {
      html = `<div class="state error" role="alert"><ha-icon icon="mdi:alert-circle-outline"></ha-icon><div><p>${escapeHtml(this._loadError)}</p><button class="pill secondary" type="button" data-act="retry">Try again</button></div></div>`;
    } else if (!this._schedules) {
      html = `<div class="state" role="status"><ha-icon icon="mdi:loading" class="spin"></ha-icon><p>Loading schedules…</p></div>`;
    } else {
      if (this._loadError) html += `<div class="inline-error" role="alert">${escapeHtml(this._loadError)}</div>`;
      if (!this._schedules.length) {
        html += `<div class="state"><ha-icon icon="mdi:calendar-blank-outline"></ha-icon><p>No schedules yet.${admin ? " Add one to clean automatically." : ""}</p></div>`;
      } else {
        const sorted = [...this._schedules].sort((a, b) => a.time.localeCompare(b.time) || a.name.localeCompare(b.name));
        html += `<ul class="schedules">${sorted.map((s) => this._rowHtml(s, admin)).join("")}</ul>`;
      }
    }
    // preserve focus on the same control across re-render
    const active = this.shadowRoot.activeElement;
    const focusSel = active && el.contains(active) ? this._selectorFor(active) : null;
    el.innerHTML = html;
    el.setAttribute("aria-busy", String(this._loading));
    if (focusSel) el.querySelector(focusSel)?.focus();
  }

  _selectorFor(node) {
    const id = node.dataset?.id;
    const act = node.dataset?.act;
    if (act) return `[data-act="${act}"]${id ? `[data-id="${CSS.escape(id)}"]` : ""}`;
    return null;
  }

  _rowHtml(s, admin) {
    const id = escapeHtml(s.id);
    const busy = this._busy.has(s.id);
    const confirming = this._confirmId === s.id;
    const err = this._rowError[s.id];
    const repeat = s.rooms.length && s.repeat > 1 ? ` · ${s.repeat}×` : "";
    return `<li class="row${s.enabled ? "" : " off"}" data-id="${id}">
  <div class="row-main">
    <div class="time">${escapeHtml(this._time(s.time))}</div>
    <div class="info">
      <div class="name">${escapeHtml(s.name || "Untitled")}</div>
      <div class="meta">${escapeHtml(this._daysText(s.days))} · ${escapeHtml(this._roomsText(s.rooms))}${repeat}</div>
    </div>
    <button class="switch" type="button" role="switch" aria-checked="${s.enabled}" aria-label="${escapeHtml(s.name || "Schedule")} enabled" data-act="toggle" data-id="${id}" ${!admin || busy ? "disabled" : ""}><span class="knob"></span></button>
  </div>
  ${admin && !confirming ? `<div class="row-actions">
    <button class="pill secondary" type="button" data-act="edit" data-id="${id}" ${busy ? "disabled" : ""}><ha-icon icon="mdi:pencil"></ha-icon><span>Edit</span></button>
    <button class="pill secondary danger" type="button" data-act="ask-delete" data-id="${id}" aria-label="Delete ${escapeHtml(s.name || "schedule")}" ${busy ? "disabled" : ""}><ha-icon icon="mdi:delete-outline"></ha-icon><span>Delete</span></button>
  </div>` : ""}
  ${confirming ? `<div class="confirm" role="alertdialog" aria-label="Confirm delete" aria-describedby="cd-${id}">
    <p id="cd-${id}">Delete “${escapeHtml(s.name || "Untitled")}”? This can’t be undone.</p>
    <div class="row-actions">
      <button class="pill secondary" type="button" data-act="cancel-delete" data-id="${id}" ${busy ? "disabled" : ""}>Keep</button>
      <button class="pill danger-solid" type="button" data-act="delete" data-id="${id}" ${busy ? "disabled" : ""}>${busy ? "Deleting…" : "Delete"}</button>
    </div>
  </div>` : ""}
  ${err ? `<div class="inline-error" role="alert" tabindex="-1" data-error-for="${id}">${escapeHtml(err)}</div>` : ""}
</li>`;
  }

  _renderEditor() {
    const d = this._draft;
    const form = this.shadowRoot.querySelector("#editor");
    if (!d) return;
    const s = d.schedule;
    const errs = d.errors || {};
    const dis = d.saving || !this._isAdmin ? "disabled" : "";
    const fieldErr = (k) => (errs[k] ? `<div class="field-error" id="e-${k}" role="alert">${escapeHtml(errs[k])}</div>` : "");
    const dayBtn = (n) =>
      `<button type="button" class="day" data-act="day" data-day="${n}" aria-pressed="${s.days.includes(n)}" aria-label="${escapeHtml(this._dayName(n, "long"))}" ${dis}>${escapeHtml(this._dayName(n, "short"))}</button>`;
    form.innerHTML = `
<h3 class="editor-title">${d.isNew ? "New schedule" : "Edit schedule"}</h3>
<div class="field">
  <label for="f-name">Name</label>
  <input id="f-name" type="text" maxlength="60" autocomplete="off" value="${escapeHtml(s.name)}" ${errs.name ? 'aria-invalid="true" aria-describedby="e-name"' : ""} ${dis}>
  ${fieldErr("name")}
</div>
<div class="field">
  <label for="f-time">Time</label>
  <input id="f-time" type="time" value="${escapeHtml(s.time)}" required ${errs.time ? 'aria-invalid="true" aria-describedby="e-time"' : ""} ${dis}>
  ${fieldErr("time")}
</div>
<fieldset class="field" id="days-group" ${errs.days ? 'aria-describedby="e-days"' : ""}>
  <legend>Days</legend>
  <div class="days">${WEEKDAYS.map(dayBtn).join("")}</div>
  <div class="presets">
    <button type="button" class="chip" data-act="preset" data-preset="all" ${dis}>Every day</button>
    <button type="button" class="chip" data-act="preset" data-preset="weekdays" ${dis}>Weekdays</button>
    <button type="button" class="chip" data-act="preset" data-preset="weekend" ${dis}>Weekends</button>
  </div>
  ${fieldErr("days")}
</fieldset>
<fieldset class="field" id="rooms-group">
  <legend>Clean</legend>
  <div id="rooms"></div>
</fieldset>
<fieldset class="field" id="repeat-group" ${d.scope === "rooms" ? "" : "hidden"}>
  <legend>Repeat</legend>
  <div class="segmented" role="radiogroup" aria-label="Number of passes">
    ${REPEATS.map((n) => `<button type="button" role="radio" aria-checked="${s.repeat === n}" data-act="repeat" data-repeat="${n}" ${dis}>${n}×</button>`).join("")}
  </div>
</fieldset>
<div class="field inline">
  <label for="f-enabled">Enabled</label>
  <button id="f-enabled" class="switch" type="button" role="switch" aria-checked="${s.enabled}" data-act="enabled" ${dis}><span class="knob"></span></button>
</div>
${d.error ? `<div class="inline-error" role="alert">${escapeHtml(d.error)}</div>` : ""}
${!this._isAdmin ? `<div class="inline-error" role="alert">Only a Home Assistant administrator can save schedules.</div>` : ""}
<div class="editor-actions">
  <button class="pill secondary" type="button" data-act="cancel" ${d.saving ? "disabled" : ""}>Cancel</button>
  <button class="pill primary" type="submit" ${dis}>${d.saving ? "Saving…" : "Save"}</button>
</div>`;
    this._renderRoomPicker();
  }

  _renderRoomPicker() {
    const box = this.shadowRoot.querySelector("#rooms");
    const d = this._draft;
    if (!box || !d) return;
    const rg = this.shadowRoot.querySelector("#repeat-group");
    if (rg) rg.hidden = d.scope !== "rooms";
    const s = d.schedule;
    const dis = d.saving || !this._isAdmin ? "disabled" : "";
    const known = this._rooms();
    const knownIds = known.map((r) => r.id);
    const missing = s.rooms.filter((id) => !knownIds.includes(id)).map((id) => ({ id, name: `Room ${id} (not found)` }));
    const all = [...known, ...missing];
    const whole = d.scope === "home";
    const roomErr = !whole && !s.rooms.length && d.errors?.rooms;
    box.innerHTML = `
<div class="segmented" role="radiogroup" aria-label="Cleaning area">
  <button type="button" role="radio" aria-checked="${whole}" data-act="scope" data-scope="home" ${dis}>Whole home</button>
  <button type="button" role="radio" aria-checked="${!whole}" data-act="scope" data-scope="rooms" ${dis}>Choose rooms</button>
</div>
${whole ? "" : all.length
      ? `<div class="rooms" role="group" aria-label="Rooms" ${roomErr ? 'aria-describedby="e-rooms"' : ""}>${all
          .map(
            (r) => `<label class="room"><input type="checkbox" data-act="room" data-room="${r.id}" ${s.rooms.includes(r.id) ? "checked" : ""} ${dis}><span>${escapeHtml(r.name)}</span></label>`,
          )
          .join("")}</div>
<div class="hint">Rooms are cleaned in your saved room order.</div>`
      : `<div class="hint">No rooms available yet. Rooms appear once the map has loaded.</div>`}
${roomErr ? `<div class="field-error" id="e-rooms" role="alert">${escapeHtml(d.errors.rooms)}</div>` : ""}`;
  }

  /* ---------------- events ---------------- */

  _onClick(e) {
    const btn = e.target.closest("button");
    if (!btn || btn.disabled) return;
    if (btn.id === "add") return this._openEditor(null);
    if (btn.id === "refresh") return this._load();
    const act = btn.dataset.act;
    const id = btn.dataset.id;
    const d = this._draft;
    switch (act) {
      case "retry": return this._load();
      case "toggle": return this._toggle(id);
      case "edit": return this._openEditor(id);
      case "ask-delete":
        this._confirmId = id;
        this._renderList();
        this.shadowRoot.querySelector(`[data-act="cancel-delete"][data-id="${CSS.escape(id)}"]`)?.focus();
        return;
      case "cancel-delete":
        this._confirmId = null;
        this._renderList();
        this.shadowRoot.querySelector(`[data-act="ask-delete"][data-id="${CSS.escape(id)}"]`)?.focus();
        return;
      case "delete": return this._delete(id);
      case "cancel":
        this._closeEditor();
        this._focusAfter = "add";
        this._restoreFocus();
        return;
    }
    if (!d || d.saving) return;
    const s = d.schedule;
    if (act === "day") {
      const n = Number(btn.dataset.day);
      s.days = s.days.includes(n) ? s.days.filter((x) => x !== n) : [...s.days, n].sort((a, b) => a - b);
      btn.setAttribute("aria-pressed", String(s.days.includes(n)));
      this._dayChanged();
    } else if (act === "preset") {
      s.days = { all: [...WEEKDAYS], weekdays: [...WEEKDAYS_ONLY], weekend: [...WEEKEND] }[btn.dataset.preset];
      this.shadowRoot.querySelectorAll("[data-act=day]").forEach((b) => b.setAttribute("aria-pressed", String(s.days.includes(Number(b.dataset.day)))));
      this._dayChanged();
    } else if (act === "repeat") {
      s.repeat = Number(btn.dataset.repeat);
      this.shadowRoot.querySelectorAll("[data-act=repeat]").forEach((b) => b.setAttribute("aria-checked", String(b === btn)));
      this._persistDraft();
    } else if (act === "enabled") {
      s.enabled = !s.enabled;
      btn.setAttribute("aria-checked", String(s.enabled));
      this._persistDraft();
    } else if (act === "scope") {
      d.scope = btn.dataset.scope;
      if (d.errors) delete d.errors.rooms;
      this._renderRoomPicker();
      this._persistDraft();
      this.shadowRoot.querySelector(`[data-act=scope][data-scope=${d.scope}]`)?.focus();
    }
  }

  _dayChanged() {
    const d = this._draft;
    if (d.errors?.days && d.schedule.days.length) {
      delete d.errors.days;
      this.shadowRoot.querySelector("#e-days")?.remove();
    }
    this._persistDraft();
  }

  _onInput(e) {
    const d = this._draft;
    const t = e.target;
    if (!d || d.saving) return;
    const s = d.schedule;
    if (t.id === "f-name" && e.type === "input") {
      s.name = t.value;
      if (d.errors?.name && t.value.trim()) {
        delete d.errors.name;
        t.removeAttribute("aria-invalid");
        this.shadowRoot.querySelector("#e-name")?.remove();
      }
    } else if (t.id === "f-time") {
      s.time = t.value;
      if (d.errors?.time && TIME_RE.test(t.value)) {
        delete d.errors.time;
        t.removeAttribute("aria-invalid");
        this.shadowRoot.querySelector("#e-time")?.remove();
      }
    } else if (t.dataset?.act === "room" && e.type === "change") {
      const id = Number(t.dataset.room);
      s.rooms = t.checked ? [...s.rooms, id] : s.rooms.filter((x) => x !== id);
      // order rooms by the saved room order, unknown ids last
      const order = this._rooms().map((r) => r.id);
      const rank = (x) => (order.indexOf(x) < 0 ? order.length + x : order.indexOf(x));
      s.rooms.sort((a, b) => rank(a) - rank(b));
      if (d.errors?.rooms && s.rooms.length) {
        delete d.errors.rooms;
        this.shadowRoot.querySelector("#e-rooms")?.remove();
      }
    } else return;
    this._persistDraft();
  }
}

/* ------------------------------------ styles ------------------------------------ */

const STYLES = `${LUCENT_TOKENS}${LUCENT_CONTROLS}
ha-card {
  position: relative;
  overflow: hidden;
  font-family: var(--lu-font);
  container-type: inline-size;
  height: 100%;
  display: flex;
  flex-direction: column;
  color: var(--lu-ink);
}
.head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 20px 6px; }
.title { display: flex; align-items: center; gap: 8px; font-size: 15px; font-weight: 600; }
.title ha-icon { --mdc-icon-size: 20px; color: var(--lu-ink-2); }
.head-actions { display: flex; align-items: center; gap: 8px; }
.body { padding: 8px 20px 18px; min-width: 0; }
.banner { margin: 4px 20px 0; padding: 10px 14px; border-radius: var(--lu-radius-control); background: var(--lu-tile); color: var(--lu-ink-2); font-size: 13px; }

.pill {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  min-height: var(--lu-target); padding: 0 20px;
  border-radius: var(--lu-radius-pill);
  font-size: 15px; font-weight: 600;
  --mdc-icon-size: 18px;
  transition: transform var(--lu-motion-press) var(--lu-ease), opacity var(--lu-motion-focus) var(--lu-ease), background var(--lu-motion-focus) var(--lu-ease);
}
.pill:active:not(:disabled) { transform: scale(.97); }
.pill.primary { border: 0; background: var(--lu-accent); color: var(--lu-accent-ink); }
.pill.primary:disabled { background: var(--lu-glass-raised); color: var(--lu-ink-3); }
.pill.secondary { padding: 0 16px; border: 1px solid var(--lu-edge); background: color-mix(in srgb, var(--lu-glass-raised) 60%, transparent); color: var(--lu-ink); font-weight: 500; }
.pill.secondary:disabled { color: var(--lu-ink-3); border-color: transparent; background: transparent; }
.pill.danger { color: var(--lu-danger); }
.pill.danger-solid { border: 0; background: var(--lu-danger); color: #fff; }
.pill.danger-solid:disabled { opacity: .6; }
.icon-button { display: grid; place-items: center; width: var(--lu-target); height: var(--lu-target); padding: 0; border-radius: 50%; border: 1px solid transparent; background: transparent; color: var(--lu-ink-2); --mdc-icon-size: 22px; }
.icon-button:hover:not(:disabled) { background: var(--lu-glass-raised); }

.state { display: flex; align-items: center; justify-content: center; gap: 12px; padding: 28px 8px; color: var(--lu-ink-2); text-align: center; --mdc-icon-size: 28px; }
.state p { margin: 0 0 10px; }
.state:not(.error) p { margin: 0; }
.state.error { color: var(--lu-ink); text-align: left; }
.state.error ha-icon { color: var(--lu-danger); }
.spin { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
.inline-error { margin: 8px 0 0; padding: 10px 14px; border-radius: var(--lu-radius-control); border: 1px solid color-mix(in srgb, var(--lu-danger) 50%, transparent); color: var(--lu-danger); font-size: 13px; }
.field-error { margin-top: 6px; color: var(--lu-danger); font-size: 13px; }

.schedules { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 10px; }
.row { padding: 12px 14px; border-radius: var(--lu-radius-tile); background: var(--lu-tile); border: 1px solid var(--lu-edge); }
.row.off .row-main .time, .row.off .info { opacity: .55; }
.row-main { display: flex; align-items: center; gap: 14px; }
.time { font-size: 22px; font-weight: 600; font-variant-numeric: tabular-nums; flex: none; min-width: 5.5ch; }
.info { flex: 1; min-width: 0; }
.name { font-size: 15px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.meta { font-size: 13px; color: var(--lu-ink-2); overflow-wrap: anywhere; }
.row-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 10px; flex-wrap: wrap; }
.confirm { margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--lu-edge); }
.confirm p { margin: 0; font-size: 14px; overflow-wrap: anywhere; }

.switch { position: relative; flex: none; width: 52px; height: 32px; padding: 0; border-radius: var(--lu-radius-pill); border: 1px solid var(--lu-edge-raised); background: var(--lu-glass-raised); transition: background var(--lu-motion-focus) var(--lu-ease); }
.switch::before { content: ""; position: absolute; inset: -8px -2px; } /* 48px touch target */
.switch .knob { position: absolute; top: 3px; left: 3px; width: 24px; height: 24px; border-radius: 50%; background: var(--lu-ink-2); transition: transform var(--lu-motion-focus) var(--lu-ease), background var(--lu-motion-focus) var(--lu-ease); }
.switch[aria-checked="true"] { background: var(--lu-accent); border-color: transparent; }
.switch[aria-checked="true"] .knob { transform: translateX(20px); background: var(--lu-accent-ink); }
.switch:disabled { opacity: .5; }

.editor { display: flex; flex-direction: column; gap: 16px; }
.editor-title { margin: 0; font-size: 17px; font-weight: 600; }
.field { margin: 0; padding: 0; border: 0; min-width: 0; display: flex; flex-direction: column; gap: 8px; }
.field.inline { flex-direction: row; align-items: center; justify-content: space-between; }
.field > label, .field > legend { font-size: 13px; font-weight: 600; color: var(--lu-ink-2); padding: 0; }
.field > legend { margin-bottom: 8px; float: left; width: 100%; }
.field > legend + * { clear: both; }
input[type="text"], input[type="time"] {
  box-sizing: border-box; width: 100%; min-height: var(--lu-target); padding: 0 14px;
  border-radius: var(--lu-radius-control); border: 1px solid var(--lu-edge); background: var(--lu-tile);
  color: var(--lu-ink); font: inherit; font-size: 16px; color-scheme: light dark;
}
input[type="time"] { width: auto; max-width: 100%; }
input:focus-visible { outline: 2px solid var(--lu-accent); outline-offset: 2px; }
input[aria-invalid="true"] { border-color: var(--lu-danger); }
input:disabled { opacity: .6; }
.days { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 6px; }
.day { min-height: var(--lu-target); padding: 0; border-radius: var(--lu-radius-control); border: 1px solid var(--lu-edge); background: var(--lu-tile); color: var(--lu-ink-2); font-size: 13px; font-weight: 500; }
.day[aria-pressed="true"] { background: var(--lu-accent); border-color: transparent; color: var(--lu-accent-ink); font-weight: 600; }
.day:disabled { opacity: .6; }
.presets { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.chip { min-height: 36px; padding: 0 14px; border-radius: var(--lu-radius-pill); border: 1px solid var(--lu-edge); background: transparent; color: var(--lu-ink-2); font-size: 13px; }
.chip:disabled { opacity: .6; }
.segmented { align-self: flex-start; max-width: 100%; }
.segmented button:disabled { opacity: .6; }
.rooms { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 6px; margin-top: 10px; }
.room { display: flex; align-items: center; gap: 10px; min-height: var(--lu-target); padding: 0 12px; border-radius: var(--lu-radius-control); border: 1px solid var(--lu-edge); background: var(--lu-tile); font-size: 14px; cursor: pointer; }
.room:has(input:checked) { border-color: var(--lu-accent); }
.room:has(input:focus-visible) { outline: 2px solid var(--lu-accent); outline-offset: 2px; }
.room input { width: 20px; height: 20px; margin: 0; accent-color: var(--lu-accent); flex: none; }
.room span { overflow-wrap: anywhere; }
.hint { margin-top: 8px; font-size: 13px; color: var(--lu-ink-3); }
.sr-only { position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }
.editor-actions { display: flex; justify-content: flex-end; gap: 8px; position: sticky; bottom: 0; padding-top: 4px; }

@container (max-width: 420px) {
  .head, .body { padding-left: 14px; padding-right: 14px; }
  .banner { margin-left: 14px; margin-right: 14px; }
  .row-main { gap: 10px; }
  .time { font-size: 19px; }
  .pill.primary span { display: none; }
  .pill.primary#add { padding: 0 14px; }
  .editor-actions .pill { flex: 1; }
  .row-actions .pill { flex: 1; }
}
`;

// The card may be served twice during a move from a /local resource to the integration; the
// first definition wins and the duplicate must not throw.
if (!customElements.get("roborock-schedule-card")) {
  customElements.define("roborock-schedule-card", RoborockScheduleCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "roborock-schedule-card",
    name: "Roborock schedules",
    description: "Create, edit, enable and delete local cleaning schedules for whole home or chosen rooms.",
    preview: false,
  });
}
