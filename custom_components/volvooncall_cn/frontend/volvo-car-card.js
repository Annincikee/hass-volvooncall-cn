const CARD_VERSION = "2.0.2";

const ENTITY_DEFINITIONS = {
  lock: ["lock", "lock"],
  window_lock: ["lock", "window_lock"],
  engine: ["binary_sensor", "engine"],
  front_left_door: ["binary_sensor", "front_left_door"],
  front_right_door: ["binary_sensor", "front_right_door"],
  rear_left_door: ["binary_sensor", "rear_left_door"],
  rear_right_door: ["binary_sensor", "rear_right_door"],
  front_left_window: ["binary_sensor", "front_left_window"],
  front_right_window: ["binary_sensor", "front_right_window"],
  rear_left_window: ["binary_sensor", "rear_left_window"],
  rear_right_window: ["binary_sensor", "rear_right_window"],
  tailgate: ["binary_sensor", "tail_gate"],
  hood: ["binary_sensor", "hood"],
  sunroof: ["binary_sensor", "sunroof"],
  fuel: ["sensor", "fuel_amount"],
  fuel_range: ["sensor", "distance_to_empty"],
  battery: ["sensor", "battery_charge_level"],
  electric_range: ["sensor", "electric_range"],
  full_charge_range: ["sensor", "full_charge_electric_range"],
  charging_status: ["sensor", "charging_status"],
  charger_connection: ["sensor", "charger_connection_status"],
  charging_power: ["sensor", "charging_power"],
  charging_time: ["sensor", "estimated_charging_time"],
  odometer: ["sensor", "odometer"],
  tm_distance: ["sensor", "tm_distance"],
  tm_fuel_consumption: ["sensor", "tm_fuel_consumption"],
  tm_energy_consumption: ["sensor", "tm_energy_consumption"],
  tm_average_speed: ["sensor", "tm_average_speed"],
  ta_distance: ["sensor", "ta_distance"],
  ta_fuel_consumption: ["sensor", "ta_fuel_consumption"],
  ta_average_speed: ["sensor", "ta_average_speed"],
  connection: ["sensor", "connection_status"],
  engine_control: ["switch", "engine_remote_control"],
  climatization: ["switch", "climatization"],
  tailgate_control: ["switch", "tailgate_control"],
  sunroof_control: ["switch", "sunroof_control"],
  flash: ["button", "flash"],
  honk_flash: ["button", "honk_and_flash"],
};

const BODY_PARTS = [
  ["hood", "引擎盖", "hood"],
  ["front_left_door", "左前门", "door fl"],
  ["front_right_door", "右前门", "door fr"],
  ["rear_left_door", "左后门", "door rl"],
  ["rear_right_door", "右后门", "door rr"],
  ["front_left_window", "左前窗", "window wfl"],
  ["front_right_window", "右前窗", "window wfr"],
  ["rear_left_window", "左后窗", "window wrl"],
  ["rear_right_window", "右后窗", "window wrr"],
  ["sunroof", "天窗", "sunroof"],
  ["tailgate", "后备箱", "tailgate"],
];

const CONTROL_DEFINITIONS = [
  ["lock", "lock", "车锁", "mdi:lock-outline"],
  ["engine_control", "switch", "远程启动", "mdi:engine-outline"],
  ["climatization", "switch", "温度调节", "mdi:air-conditioner"],
  ["tailgate_control", "switch", "后备箱", "mdi:car-back"],
  ["sunroof_control", "switch", "天窗", "mdi:home-roof"],
  ["flash", "button", "闪灯", "mdi:car-light-high"],
  ["honk_flash", "button", "鸣笛闪灯", "mdi:alarm-light-outline"],
];

const LABELS = {
  vin: "车辆 VIN",
  name: "卡片标题",
  model: "车型",
  image: "车辆图片 URL（可选，仅支持 /local/... 或 HTTPS）",
  show_controls: "显示远程控制",
  show_statistics: "显示行程统计",
};

const MODEL_LABELS = {
  s90_t8: "S90 Recharge T8",
  s90: "S90",
  xc60_t8: "XC60 Recharge T8",
  xc90_t8: "XC90 Recharge T8",
  generic: "Volvo",
};

class VolvoCarCard extends HTMLElement {
  static getConfigForm() {
    return {
      schema: [
        { name: "vin", required: true, selector: { text: {} } },
        { name: "name", selector: { text: {} } },
        {
          name: "model",
          selector: {
            select: {
              mode: "dropdown",
              options: [
                { value: "s90_t8", label: "S90 T8" },
                { value: "s90", label: "S90" },
                { value: "xc60_t8", label: "XC60 T8" },
                { value: "xc90_t8", label: "XC90 T8" },
                { value: "generic", label: "其他车型" },
              ],
            },
          },
        },
        { name: "image", selector: { text: {} } },
        { name: "show_controls", selector: { boolean: {} } },
        { name: "show_statistics", selector: { boolean: {} } },
      ],
      computeLabel: (schema) => LABELS[schema.name] || schema.name,
    };
  }

  static getStubConfig() {
    return {
      vin: "",
      name: "S90 T8",
      model: "s90_t8",
      show_controls: true,
      show_statistics: true,
    };
  }

  setConfig(config) {
    this._config = {
      name: "S90 T8",
      model: "s90_t8",
      show_controls: true,
      show_statistics: true,
      entities: {},
      ...config,
    };
    this._lastStateSignature = undefined;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (this.shadowRoot?.querySelector(".confirm-dialog[open]")) return;
    const signature = this._stateSignature(hass);
    if (signature === this._lastStateSignature && this.shadowRoot) return;
    this._lastStateSignature = signature;
    this._render();
  }

  getCardSize() {
    return this._config?.show_statistics === false ? 8 : 10;
  }

  getGridOptions() {
    return {
      rows: this._config?.show_statistics === false ? 8 : 10,
      columns: 12,
      min_rows: 7,
      min_columns: 6,
    };
  }

  connectedCallback() {
    this._render();
  }

  _vin() {
    return String(this._config?.vin || "")
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9_]/g, "");
  }

  _entityId(key) {
    const override = this._config?.entities?.[key];
    if (override) return override;
    const definition = ENTITY_DEFINITIONS[key];
    const vin = this._vin();
    return definition && vin
      ? `${definition[0]}.${vin}_${definition[1]}`
      : undefined;
  }

  _state(key) {
    const entityId = this._entityId(key);
    return entityId ? this._hass?.states?.[entityId] : undefined;
  }

  _stateSignature(hass) {
    if (!this._config) return "unconfigured";
    return Object.keys(ENTITY_DEFINITIONS)
      .map((key) => {
        const entityId = this._entityId(key);
        const state = entityId ? hass?.states?.[entityId] : undefined;
        const attrs = state?.attributes || {};
        return [
          entityId || "",
          state?.state || "missing",
          attrs.sample_count || "",
          attrs.sampled_at || "",
        ].join(":");
      })
      .join("|");
  }

  _isOn(key) {
    return this._state(key)?.state === "on";
  }

  _isAvailable(key) {
    const state = this._state(key)?.state;
    return state !== undefined && state !== "unavailable" && state !== "unknown";
  }

  _displayState(key, fallback = "—") {
    const stateObj = this._state(key);
    if (!stateObj || stateObj.state === "unknown" || stateObj.state === "unavailable") {
      return fallback;
    }
    if (this._hass?.formatEntityState) {
      return this._hass.formatEntityState(stateObj);
    }
    const unit = stateObj.attributes?.unit_of_measurement;
    return `${stateObj.state}${unit ? ` ${unit}` : ""}`;
  }

  _stateNumber(key) {
    const value = Number.parseFloat(this._state(key)?.state);
    return Number.isFinite(value) ? value : undefined;
  }

  _openParts() {
    return BODY_PARTS.filter(([key]) => this._isOn(key));
  }

  _imageUrl() {
    const configured = String(this._config?.image || "").trim();
    if (
      configured.startsWith("/") ||
      configured.startsWith("https://")
    ) {
      return configured;
    }
    return "";
  }

  _escape(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  _render() {
    if (!this.isConnected || !this._config) return;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });

    const vin = this._vin();
    if (!vin) {
      this.shadowRoot.innerHTML = `${this._styles()}
        <ha-card class="setup-card">
          <ha-icon icon="mdi:car-cog"></ha-icon>
          <div><strong>配置 Volvo 车辆卡片</strong><span>请填写 VIN。</span></div>
        </ha-card>`;
      return;
    }

    const modelName = MODEL_LABELS[this._config.model] || this._config.model || "Volvo";
    const title = this._config.name || modelName;
    const openParts = this._openParts();
    const isLocked = this._state("lock")?.state === "locked";
    const battery = this._stateNumber("battery");
    const fuel = this._stateNumber("fuel");
    const connection = String(this._state("connection")?.state || "").toLowerCase();
    const isOnline = !["disconnected", "offline", "false"].includes(connection);
    const charging = this._isCharging();
    const imageUrl = this._imageUrl();

    this.shadowRoot.innerHTML = `${this._styles()}
      <ha-card>
        <div class="hero">
          <div class="identity">
            <span class="eyebrow">Volvo Cars</span>
            <h2>${this._escape(title)}</h2>
            <button class="link-value" data-more-info="${this._escape(this._entityId("odometer"))}">
              ${this._escape(this._displayState("odometer"))}
            </button>
          </div>
          <div class="status-stack">
            <span class="connection ${isOnline ? "" : "offline"}">
              <span></span>${isOnline ? "已连接" : "离线"}
            </span>
            <button class="lock-pill ${isLocked ? "locked" : "unlocked"}"
                    data-action="lock"
                    ${this._isAvailable("lock") ? "" : "disabled"}
                    aria-label="${isLocked ? "解锁车辆" : "锁定车辆"}">
              <ha-icon icon="${isLocked ? "mdi:lock" : "mdi:lock-open-variant"}"></ha-icon>
              <span>${isLocked ? "已锁定" : "未锁定"}</span>
            </button>
          </div>
        </div>

        <div class="range-band">
          ${this._rangeTile("electric_range", "纯电续航", "mdi:lightning-bolt", "electric")}
          ${this._rangeTile("fuel_range", "燃油续航", "mdi:gas-station", "fuel")}
        </div>

        <div class="energy-grid">
          ${this._levelTile("battery", "动力电池", battery, "%", "mdi:battery-high")}
          ${this._levelTile("fuel", "燃油余量", fuel, "L", "mdi:fuel")}
          ${this._metricTile("charging_power", "充电功率", "mdi:ev-station")}
          ${this._metricTile("charging_time", "预计充满", "mdi:timer-outline")}
        </div>

        <div class="vehicle-area">
          <div class="vehicle-visual">
            <div class="car-canvas ${openParts.length ? "has-warning" : ""}">
              <div class="fallback-car" aria-hidden="true">
                <span class="fallback-shadow"></span>
                <span class="fallback-body"></span>
                <span class="fallback-hood"></span>
                <span class="fallback-glass front"></span>
                <span class="fallback-glass rear"></span>
                <span class="fallback-roof"></span>
                <span class="fallback-tail"></span>
              </div>
              ${
                imageUrl
                  ? `<img src="${this._escape(imageUrl)}" alt="Volvo vehicle" />`
                  : ""
              }
              ${BODY_PARTS.map((part) => this._partOverlay(part)).join("")}
              <div class="center-badge ${openParts.length ? "warning" : ""}">
                <ha-icon icon="${
                  openParts.length
                    ? "mdi:alert-circle"
                    : isLocked
                      ? "mdi:shield-lock"
                      : "mdi:check-circle"
                }"></ha-icon>
                <span>${this._escape(this._vehicleSummary(isLocked, openParts))}</span>
              </div>
            </div>
          </div>
          <div class="state-panel">
            ${this._stateRow("lock", "车辆锁", isLocked ? "已锁定" : "未锁定", isLocked ? "ok" : "warn")}
            ${this._stateRow("charging_status", "充电状态", this._displayState("charging_status"), charging ? "charge" : "")}
            ${this._stateRow("full_charge_range", "最近满电续航", this._displayState("full_charge_range"), "charge")}
            ${this._stateRow("engine", "发动机", this._isOn("engine") ? "运行中" : "关闭", this._isOn("engine") ? "warn" : "")}
            <div class="open-list">
              <span>开口状态</span>
              <div>
                ${
                  openParts.length
                    ? openParts
                        .map(([key, label]) => `<button data-more-info="${this._escape(this._entityId(key))}">${label}</button>`)
                        .join("")
                    : "<em>全部关闭</em>"
                }
              </div>
            </div>
          </div>
        </div>

        ${
          this._config.show_statistics === false
            ? ""
            : `<div class="statistics">
                <div class="section-title">
                  <span>行程统计</span>
                  <small>TM 手动复位 · TA 自动复位</small>
                </div>
                <div class="trip-grid">
                  ${this._tripBlock("TM", [
                    ["tm_distance", "里程"],
                    ["tm_average_speed", "均速"],
                    ["tm_fuel_consumption", "油耗"],
                    ["tm_energy_consumption", "电耗"],
                  ])}
                  ${this._tripBlock("TA", [
                    ["ta_distance", "里程"],
                    ["ta_average_speed", "均速"],
                    ["ta_fuel_consumption", "油耗"],
                  ])}
                </div>
              </div>`
        }

        ${
          this._config.show_controls === false
            ? ""
            : `<div class="controls-wrap">
                <div class="section-title">
                  <span>远程控制</span>
                  <small>关键操作会二次确认</small>
                </div>
                <div class="controls">
                  ${CONTROL_DEFINITIONS.map((control) => this._control(control)).join("")}
                </div>
              </div>`
        }

        <dialog class="confirm-dialog">
          <div class="dialog-icon"><ha-icon icon="mdi:car-key"></ha-icon></div>
          <h3>确认车辆操作</h3>
          <p></p>
          <div class="dialog-actions">
            <button class="dialog-cancel">取消</button>
            <button class="dialog-confirm">确认</button>
          </div>
        </dialog>
        <div class="error" hidden></div>
      </ha-card>`;

    this._bindEvents();
  }

  _isCharging() {
    const chargingState = String(this._state("charging_status")?.state || "").toLowerCase();
    const connectorState = String(this._state("charger_connection")?.state || "").toLowerCase();
    return (
      chargingState.includes("charg") ||
      connectorState.includes("charg") ||
      connectorState.includes("connected")
    );
  }

  _vehicleSummary(isLocked, openParts) {
    if (openParts.length) return `${openParts.length} 处未关闭`;
    return isLocked ? "车辆已锁闭" : "车门车窗已关闭";
  }

  _rangeTile(key, label, icon, tone) {
    const value = this._displayState(key);
    return `
      <button class="range-tile ${tone}" data-more-info="${this._escape(this._entityId(key))}">
        <span class="range-icon"><ha-icon icon="${icon}"></ha-icon></span>
        <span class="range-copy">
          <strong>${this._escape(value)}</strong>
          <span>${label}</span>
        </span>
      </button>`;
  }

  _levelTile(key, label, value, unit, icon) {
    const numeric = Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0;
    const display = this._displayState(key);
    return `
      <button class="level-tile" data-more-info="${this._escape(this._entityId(key))}">
        <span class="tile-heading"><ha-icon icon="${icon}"></ha-icon>${label}</span>
        <strong>${this._escape(display)}</strong>
        <span class="level-track" aria-hidden="true"><span style="width:${numeric}%"></span></span>
      </button>`;
  }

  _metricTile(key, label, icon) {
    return `
      <button class="level-tile compact ${this._isAvailable(key) ? "" : "unavailable"}"
              data-more-info="${this._escape(this._entityId(key))}">
        <span class="tile-heading"><ha-icon icon="${icon}"></ha-icon>${label}</span>
        <strong>${this._escape(this._displayState(key))}</strong>
      </button>`;
  }

  _partOverlay(part) {
    const [key, label, className] = part;
    const isOpen = this._isOn(key);
    const entityId = this._entityId(key) || "";
    return `
      <button class="part ${className} ${isOpen ? "open" : "closed"}"
              aria-label="${label}${isOpen ? "已打开" : "已关闭"}"
              title="${label} · ${isOpen ? "已打开" : "已关闭"}"
              data-more-info="${this._escape(entityId)}">
        <span class="part-dot"></span>
        ${isOpen ? `<span class="part-label">${label}</span>` : ""}
      </button>`;
  }

  _stateRow(key, label, value, tone) {
    return `<button class="state-row ${tone || ""} ${this._isAvailable(key) ? "" : "missing"}"
                    data-more-info="${this._escape(this._entityId(key))}">
              <span>${label}</span><strong>${this._escape(value)}</strong>
            </button>`;
  }

  _tripBlock(title, rows) {
    return `
      <div class="trip-block">
        <span class="trip-title">${title}</span>
        <div class="trip-metrics">
          ${rows
            .map(([key, label]) => `
              <button data-more-info="${this._escape(this._entityId(key))}">
                <strong>${this._escape(this._displayState(key))}</strong>
                <span>${label}</span>
              </button>`)
            .join("")}
        </div>
      </div>`;
  }

  _control(control) {
    const [key, kind, label, icon] = control;
    const stateObj = this._state(key);
    const available = this._isAvailable(key);
    const active =
      kind === "lock" ? stateObj?.state === "locked" : stateObj?.state === "on";
    const dynamicLabel =
      kind === "lock" ? (active ? "已锁车" : "未锁车") : label;
    return `
      <button class="control ${active ? "active" : ""}"
              data-action="${key}"
              ${available ? "" : "disabled"}
              aria-label="${dynamicLabel}">
        <span class="control-icon"><ha-icon icon="${icon}"></ha-icon></span>
        <span>${dynamicLabel}</span>
      </button>`;
  }

  _bindEvents() {
    const image = this.shadowRoot.querySelector(".car-canvas img");
    image?.addEventListener("error", () => {
      image.hidden = true;
      this.shadowRoot.querySelector(".car-canvas")?.classList.add("image-failed");
    });
    this.shadowRoot.querySelectorAll("[data-more-info]").forEach((element) => {
      element.addEventListener("click", () => {
        const entityId = element.dataset.moreInfo;
        if (!entityId || !this._hass?.states?.[entityId]) return;
        this.dispatchEvent(
          new CustomEvent("hass-more-info", {
            detail: { entityId },
            bubbles: true,
            composed: true,
          }),
        );
      });
    });
    this.shadowRoot.querySelectorAll("[data-action]").forEach((element) => {
      element.addEventListener("click", () => this._runAction(element.dataset.action));
    });
  }

  async _runAction(key) {
    const entityId = this._entityId(key);
    const stateObj = entityId ? this._hass?.states?.[entityId] : undefined;
    if (!entityId || !stateObj) return;

    let domain;
    let service;
    let message;
    if (key === "lock") {
      domain = "lock";
      service = stateObj.state === "locked" ? "unlock" : "lock";
      message = service === "unlock" ? "确认解锁车辆？" : null;
    } else if (stateObj.entity_id.startsWith("switch.")) {
      domain = "switch";
      service = stateObj.state === "on" ? "turn_off" : "turn_on";
      const actionName = service === "turn_on" ? "开启" : "关闭";
      const label = CONTROL_DEFINITIONS.find(([controlKey]) => controlKey === key)?.[2] || "设备";
      message = `确认${actionName}${label}？`;
    } else if (stateObj.entity_id.startsWith("button.")) {
      domain = "button";
      service = "press";
      message = key === "honk_flash" ? "确认执行鸣笛并闪灯？" : null;
    } else {
      return;
    }

    if (message && !(await this._confirm(message))) return;
    try {
      await this._hass.callService(domain, service, { entity_id: entityId });
      this._showError("");
    } catch (error) {
      this._showError(`操作失败：${error?.message || error}`);
    }
  }

  _confirm(message) {
    const dialog = this.shadowRoot?.querySelector(".confirm-dialog");
    if (!dialog?.showModal) return Promise.resolve(window.confirm(message));

    dialog.querySelector("p").textContent = message;
    dialog.showModal();
    return new Promise((resolve) => {
      const cancel = dialog.querySelector(".dialog-cancel");
      const confirm = dialog.querySelector(".dialog-confirm");
      const finish = (result) => {
        dialog.close();
        cancel.removeEventListener("click", onCancel);
        confirm.removeEventListener("click", onConfirm);
        dialog.removeEventListener("cancel", onCancel);
        resolve(result);
      };
      const onCancel = (event) => {
        event?.preventDefault();
        finish(false);
      };
      const onConfirm = () => finish(true);
      cancel.addEventListener("click", onCancel);
      confirm.addEventListener("click", onConfirm);
      dialog.addEventListener("cancel", onCancel);
    });
  }

  _showError(message) {
    const element = this.shadowRoot?.querySelector(".error");
    if (!element) return;
    element.hidden = !message;
    element.textContent = message;
  }

  _styles() {
    return `<style>
      :host {
        --voc-text: var(--primary-text-color, #141414);
        --voc-secondary: var(--secondary-text-color, #707070);
        --voc-bg: var(--ha-card-background, var(--card-background-color, #fff));
        --voc-surface: color-mix(in srgb, var(--voc-bg) 94%, var(--voc-text));
        --voc-subtle: color-mix(in srgb, var(--voc-bg) 88%, var(--voc-text));
        --voc-line: color-mix(in srgb, var(--voc-text) 14%, transparent);
        --voc-blue: #1c6eba;
        --voc-brand: #284e80;
        --voc-warning: #cd2314;
        --voc-positive: #04721c;
        display: block;
        color: var(--voc-text);
        font-family: var(--ha-font-family-body, "Helvetica Neue", Arial, sans-serif);
      }
      * { box-sizing: border-box; }
      button { font: inherit; }
      ha-card {
        display: block;
        overflow: hidden;
        background: var(--voc-bg);
        border-radius: var(--ha-card-border-radius, 14px);
      }
      .hero {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 16px;
        padding: 22px 22px 14px;
      }
      .identity { min-width: 0; }
      .eyebrow {
        display: block;
        margin-bottom: 4px;
        color: var(--voc-secondary);
        font-size: 10px;
        font-weight: 600;
        text-transform: uppercase;
      }
      h2 {
        margin: 0;
        font-size: 24px;
        font-weight: 400;
        letter-spacing: 0;
        line-height: 1.15;
      }
      .link-value {
        border: 0;
        padding: 5px 0 0;
        background: none;
        color: var(--voc-secondary);
        cursor: pointer;
        font-size: 12px;
      }
      .status-stack { display: flex; align-items: center; gap: 10px; }
      .connection {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        color: var(--voc-secondary);
        font-size: 11px;
        white-space: nowrap;
      }
      .connection > span { width: 6px; height: 6px; border-radius: 50%; background: var(--voc-positive); }
      .connection.offline > span { background: var(--voc-warning); }
      .lock-pill {
        min-height: 38px;
        border: 0;
        border-radius: 19px;
        padding: 0 12px;
        display: inline-flex;
        align-items: center;
        gap: 7px;
        background: var(--voc-surface);
        color: var(--voc-text);
        cursor: pointer;
        font-size: 12px;
      }
      .lock-pill ha-icon { --mdc-icon-size: 17px; }
      .lock-pill.locked { background: var(--voc-text); color: var(--voc-bg); }
      .lock-pill:disabled { opacity: .4; cursor: default; }
      .range-band {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 1px;
        margin: 0 22px;
        background: var(--voc-line);
        border-block: 1px solid var(--voc-line);
      }
      .range-tile {
        min-width: 0;
        min-height: 78px;
        border: 0;
        padding: 14px 16px;
        display: flex;
        align-items: center;
        gap: 12px;
        background: var(--voc-bg);
        color: var(--voc-text);
        text-align: left;
        cursor: pointer;
      }
      .range-icon {
        width: 38px;
        height: 38px;
        border-radius: 50%;
        display: grid;
        place-items: center;
        background: var(--voc-surface);
        color: var(--voc-blue);
        flex: 0 0 auto;
      }
      .range-icon ha-icon { --mdc-icon-size: 19px; }
      .range-copy { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
      .range-copy strong { overflow: hidden; font-size: 22px; font-weight: 400; text-overflow: ellipsis; white-space: nowrap; }
      .range-copy span { color: var(--voc-secondary); font-size: 11px; }
      .range-tile.fuel .range-icon { color: #735f32; }
      .energy-grid {
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        gap: 8px;
        padding: 14px 22px 10px;
      }
      .level-tile {
        min-width: 0;
        min-height: 84px;
        border: 0;
        border-radius: 10px;
        padding: 11px;
        display: flex;
        flex-direction: column;
        justify-content: space-between;
        background: var(--voc-surface);
        color: var(--voc-text);
        text-align: left;
        cursor: pointer;
      }
      .level-tile.compact { min-height: 72px; }
      .level-tile.unavailable { opacity: .45; }
      .tile-heading {
        display: flex;
        align-items: center;
        gap: 6px;
        color: var(--voc-secondary);
        font-size: 10px;
      }
      .tile-heading ha-icon { --mdc-icon-size: 15px; color: var(--voc-blue); }
      .level-tile strong {
        overflow: hidden;
        font-size: 16px;
        font-weight: 500;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .level-track {
        height: 3px;
        overflow: hidden;
        border-radius: 999px;
        background: color-mix(in srgb, var(--voc-text) 12%, transparent);
      }
      .level-track span {
        display: block;
        height: 100%;
        border-radius: inherit;
        background: var(--voc-blue);
      }
      .vehicle-area {
        display: grid;
        grid-template-columns: minmax(180px, .82fr) minmax(210px, 1.18fr);
        gap: 14px;
        padding: 10px 22px 18px;
      }
      .vehicle-visual { min-width: 0; display: flex; justify-content: center; }
      .car-canvas {
        position: relative;
        width: min(100%, 220px);
        aspect-ratio: 1248 / 2687;
        isolation: isolate;
      }
      .fallback-car {
        position: absolute;
        inset: 0;
        z-index: 0;
        pointer-events: none;
      }
      .fallback-car span { position: absolute; display: block; }
      .fallback-shadow {
        left: 24%;
        top: 8%;
        width: 52%;
        height: 86%;
        border-radius: 48% 48% 40% 40%;
        background: radial-gradient(ellipse at center, rgba(0,0,0,.18), rgba(0,0,0,0) 68%);
        filter: blur(12px);
      }
      .fallback-body {
        left: 27%;
        top: 5%;
        width: 46%;
        height: 90%;
        border: 1px solid color-mix(in srgb, var(--voc-text) 18%, transparent);
        border-radius: 48% 48% 38% 38% / 10% 10% 8% 8%;
        background:
          linear-gradient(90deg, rgba(255,255,255,.18), transparent 22%, transparent 78%, rgba(0,0,0,.08)),
          linear-gradient(180deg, #f7f8fa 0%, #d9dde3 42%, #c7cdd5 100%);
        box-shadow: inset 0 0 0 8px rgba(255,255,255,.16), inset 0 -32px 38px rgba(0,0,0,.08);
      }
      .fallback-hood {
        left: 31%;
        top: 13%;
        width: 38%;
        height: 21%;
        border-radius: 44% 44% 18% 18%;
        background: linear-gradient(180deg, rgba(255,255,255,.45), rgba(255,255,255,.06));
        border-bottom: 1px solid rgba(0,0,0,.1);
      }
      .fallback-glass {
        left: 34%;
        width: 32%;
        border: 1px solid rgba(255,255,255,.38);
        background: linear-gradient(180deg, #70849b, #2c3946);
        opacity: .88;
      }
      .fallback-glass.front {
        top: 35%;
        height: 14%;
        border-radius: 42% 42% 10% 10%;
      }
      .fallback-glass.rear {
        top: 61%;
        height: 12%;
        border-radius: 10% 10% 38% 38%;
      }
      .fallback-roof {
        left: 35%;
        top: 48%;
        width: 30%;
        height: 13%;
        border-radius: 24%;
        background: linear-gradient(180deg, #dfe4ea, #b6bec8);
        box-shadow: inset 0 0 0 1px rgba(0,0,0,.08);
      }
      .fallback-tail {
        left: 32%;
        top: 78%;
        width: 36%;
        height: 10%;
        border-top: 1px solid rgba(0,0,0,.1);
        border-radius: 0 0 42% 42%;
        background: linear-gradient(180deg, rgba(255,255,255,.08), rgba(0,0,0,.08));
      }
      .car-canvas img {
        position: relative;
        z-index: 1;
        width: 100%;
        height: 100%;
        object-fit: contain;
        display: block;
        filter: drop-shadow(0 14px 16px rgba(0,0,0,.12));
      }
      .car-canvas img[hidden] { display: none; }
      .part {
        position: absolute;
        z-index: 2;
        border: 1px solid transparent;
        padding: 0;
        background: transparent;
        color: #fff;
        cursor: pointer;
      }
      .part.open {
        border-color: color-mix(in srgb, var(--voc-warning) 85%, white);
        background: color-mix(in srgb, var(--voc-warning) 23%, transparent);
        box-shadow: 0 0 18px color-mix(in srgb, var(--voc-warning) 42%, transparent), inset 0 0 12px rgba(255,255,255,.18);
      }
      .part-dot {
        position: absolute;
        width: 8px;
        height: 8px;
        border: 2px solid #fff;
        border-radius: 50%;
        background: var(--voc-warning);
        box-shadow: 0 1px 5px rgba(0,0,0,.38);
        opacity: 0;
      }
      .part.open .part-dot { opacity: 1; }
      .part-label {
        position: absolute;
        z-index: 3;
        padding: 3px 6px;
        border-radius: 8px;
        background: var(--voc-warning);
        box-shadow: 0 3px 10px rgba(0,0,0,.22);
        font-size: 9px;
        font-weight: 600;
        white-space: nowrap;
      }
      .hood { left: 22%; top: 12%; width: 56%; height: 22%; clip-path: polygon(12% 5%, 88% 5%, 100% 88%, 0 88%); }
      .hood .part-dot { right: 45%; bottom: 8%; }
      .hood .part-label { left: 50%; bottom: -2px; transform: translate(-50%, 100%); }
      .door { width: 29%; height: 18%; }
      .door.fl { left: 6%; top: 37%; clip-path: polygon(20% 0, 100% 8%, 94% 100%, 2% 92%); }
      .door.fr { right: 6%; top: 37%; clip-path: polygon(0 8%, 80% 0, 98% 92%, 6% 100%); }
      .door.rl { left: 7%; top: 55%; clip-path: polygon(2% 8%, 94% 0, 100% 92%, 20% 100%); }
      .door.rr { right: 7%; top: 55%; clip-path: polygon(6% 0, 98% 8%, 80% 100%, 0 92%); }
      .door.fl .part-dot, .door.rl .part-dot { left: 5%; top: 43%; }
      .door.fr .part-dot, .door.rr .part-dot { right: 5%; top: 43%; }
      .door.fl .part-label, .door.rl .part-label { left: 0; top: 50%; transform: translate(-88%, -50%); }
      .door.fr .part-label, .door.rr .part-label { right: 0; top: 50%; transform: translate(88%, -50%); }
      .window { width: 13%; height: 16%; border-radius: 40%; }
      .window.wfl { left: 23%; top: 35%; }
      .window.wfr { right: 23%; top: 35%; }
      .window.wrl { left: 24%; top: 53%; }
      .window.wrr { right: 24%; top: 53%; }
      .window .part-dot { left: 50%; top: 50%; transform: translate(-50%, -50%); }
      .window .part-label { left: 50%; top: 50%; transform: translate(-50%, -50%); }
      .sunroof { left: 34%; top: 42%; width: 32%; height: 24%; border-radius: 32% 32% 22% 22%; }
      .sunroof .part-dot { left: calc(50% - 4px); top: 10%; }
      .sunroof .part-label { left: 50%; top: 50%; transform: translate(-50%, -50%); }
      .tailgate { left: 22%; top: 78%; width: 56%; height: 14%; clip-path: polygon(0 10%, 100% 10%, 90% 94%, 10% 94%); }
      .tailgate .part-dot { left: calc(50% - 4px); top: 8%; }
      .tailgate .part-label { left: 50%; top: 0; transform: translate(-50%, -95%); }
      .center-badge {
        position: absolute;
        z-index: 4;
        left: 50%;
        top: 68%;
        display: flex;
        align-items: center;
        gap: 5px;
        padding: 6px 8px;
        border: 1px solid rgba(255,255,255,.24);
        border-radius: 9px;
        transform: translate(-50%, -50%);
        background: rgba(16,18,21,.79);
        box-shadow: 0 5px 16px rgba(0,0,0,.2);
        color: #fff;
        backdrop-filter: blur(8px);
        font-size: 9px;
        white-space: nowrap;
        pointer-events: none;
      }
      .center-badge ha-icon { --mdc-icon-size: 13px; color: #74c69a; }
      .center-badge.warning ha-icon { color: #ff766b; }
      .state-panel {
        align-self: center;
        min-width: 0;
        border-block: 1px solid var(--voc-line);
      }
      .state-row {
        width: 100%;
        min-height: 38px;
        border: 0;
        border-bottom: 1px solid var(--voc-line);
        padding: 8px 0;
        display: flex;
        justify-content: space-between;
        gap: 10px;
        background: transparent;
        color: var(--voc-secondary);
        cursor: pointer;
        font-size: 11px;
      }
      .state-row strong {
        overflow: hidden;
        color: var(--voc-text);
        font-weight: 500;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .state-row.warn strong { color: var(--voc-warning); }
      .state-row.charge strong { color: var(--voc-blue); }
      .state-row.missing { opacity: .45; cursor: default; }
      .open-list { padding: 11px 0 10px; }
      .open-list > span { display: block; margin-bottom: 7px; color: var(--voc-secondary); font-size: 10px; }
      .open-list > div { display: flex; flex-wrap: wrap; gap: 6px; }
      .open-list button,
      .open-list em {
        border: 0;
        border-radius: 8px;
        padding: 4px 7px;
        background: color-mix(in srgb, var(--voc-warning) 11%, transparent);
        color: var(--voc-warning);
        font-size: 10px;
        font-style: normal;
      }
      .open-list em { background: var(--voc-surface); color: var(--voc-positive); }
      .statistics { border-top: 1px solid var(--voc-line); padding: 14px 22px 16px; }
      .section-title {
        display: flex;
        align-items: baseline;
        justify-content: space-between;
        gap: 10px;
        margin-bottom: 10px;
      }
      .section-title > span { font-size: 13px; font-weight: 500; }
      .section-title small { color: var(--voc-secondary); font-size: 10px; }
      .trip-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
      .trip-block {
        min-width: 0;
        border-radius: 10px;
        padding: 11px;
        background: var(--voc-surface);
      }
      .trip-title { display: block; margin-bottom: 9px; color: var(--voc-secondary); font-size: 11px; font-weight: 600; }
      .trip-metrics { display: grid; grid-template-columns: 1fr 1fr; gap: 7px; }
      .trip-metrics button {
        min-width: 0;
        border: 0;
        border-radius: 8px;
        padding: 8px;
        display: flex;
        flex-direction: column;
        gap: 2px;
        background: var(--voc-bg);
        color: var(--voc-text);
        text-align: left;
        cursor: pointer;
      }
      .trip-metrics strong { overflow: hidden; font-size: 13px; font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }
      .trip-metrics span { color: var(--voc-secondary); font-size: 10px; }
      .controls-wrap { border-top: 1px solid var(--voc-line); padding: 14px 22px 20px; }
      .controls { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 8px; }
      .control {
        min-width: 0;
        min-height: 64px;
        border: 0;
        border-radius: 10px;
        padding: 8px 4px;
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 6px;
        background: var(--voc-surface);
        color: var(--voc-secondary);
        cursor: pointer;
        font-size: 10px;
      }
      .control-icon {
        width: 30px;
        height: 30px;
        display: grid;
        place-items: center;
        border-radius: 50%;
        background: var(--voc-bg);
        color: var(--voc-text);
      }
      .control-icon ha-icon { --mdc-icon-size: 17px; }
      .control.active { color: var(--voc-blue); }
      .control.active .control-icon { background: var(--voc-blue); color: #fff; }
      .control:disabled { opacity: .35; cursor: default; }
      .confirm-dialog {
        width: min(88vw, 330px);
        border: 0;
        border-radius: 14px;
        padding: 24px;
        background: var(--voc-bg);
        box-shadow: 0 20px 60px rgba(0,0,0,.32);
        color: var(--voc-text);
        text-align: center;
      }
      .confirm-dialog::backdrop { background: rgba(0,0,0,.48); backdrop-filter: blur(3px); }
      .dialog-icon { width: 46px; height: 46px; margin: 0 auto 13px; border-radius: 50%; display: grid; place-items: center; background: var(--voc-surface); color: var(--voc-blue); }
      .dialog-icon ha-icon { --mdc-icon-size: 23px; }
      .confirm-dialog h3 { margin: 0; font-size: 17px; font-weight: 500; }
      .confirm-dialog p { margin: 8px 0 20px; color: var(--voc-secondary); font-size: 13px; line-height: 1.5; }
      .dialog-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 9px; }
      .dialog-actions button { min-height: 42px; border: 0; border-radius: 10px; cursor: pointer; }
      .dialog-cancel { background: var(--voc-surface); color: var(--voc-text); }
      .dialog-confirm { background: var(--voc-blue); color: #fff; }
      .error { margin: -8px 22px 16px; border-radius: 9px; padding: 8px 10px; background: color-mix(in srgb, var(--voc-warning) 12%, transparent); color: var(--voc-warning); font-size: 11px; }
      .setup-card { min-height: 130px; padding: 24px; display: flex; align-items: center; gap: 14px; }
      .setup-card > ha-icon { --mdc-icon-size: 34px; color: var(--voc-blue); }
      .setup-card div { display: flex; flex-direction: column; gap: 4px; }
      .setup-card strong { font-weight: 500; }
      .setup-card span { color: var(--voc-secondary); font-size: 12px; }
      button:focus-visible { outline: 2px solid var(--voc-blue); outline-offset: 2px; }
      @media (max-width: 620px) {
        .hero { padding: 18px 16px 12px; }
        h2 { font-size: 21px; }
        .connection { display: none; }
        .lock-pill span { display: none; }
        .range-band { margin: 0 16px; }
        .range-tile { min-height: 70px; padding: 12px; }
        .range-copy strong { font-size: 19px; }
        .energy-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); padding: 12px 16px 8px; }
        .vehicle-area { grid-template-columns: minmax(0, 1fr) minmax(136px, .8fr); gap: 8px; padding: 8px 16px 16px; }
        .car-canvas { width: min(100%, 194px); }
        .trip-grid { grid-template-columns: 1fr; }
        .controls { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      }
      @media (max-width: 380px) {
        .range-band { grid-template-columns: 1fr; }
        .vehicle-area { grid-template-columns: 1fr; }
        .car-canvas { width: 186px; }
        .state-panel { width: 100%; }
      }
    </style>`;
  }
}

if (!customElements.get("volvo-car-card")) {
  customElements.define("volvo-car-card", VolvoCarCard);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === "volvo-car-card")) {
  window.customCards.push({
    type: "volvo-car-card",
    name: "Volvo 原生风格统计卡",
    description: "移动端优先的车辆状态、双能源续航、TM/TA 行程统计与远程控制卡。",
    preview: true,
    documentationURL: "https://github.com/idreamshen/hass-volvooncall-cn",
    getEntitySuggestion: (_hass, entityId) => {
      const match = entityId.match(
        /^(?:lock|sensor|binary_sensor|switch)\.([a-z0-9]+)_(?:lock|engine|climatization|battery_charge_level|full_charge_electric_range|tm_distance|fuel_amount)$/,
      );
      if (!match) return null;
      return {
        config: {
          type: "custom:volvo-car-card",
          vin: match[1].toUpperCase(),
          name: "S90 T8",
          model: "s90_t8",
          show_controls: true,
          show_statistics: true,
        },
      };
    },
  });
}

console.info(
  `%c VOLVO-CAR-CARD %c v${CARD_VERSION} `,
  "color:#fff;background:#284e80;padding:3px 5px",
  "color:#284e80;background:#e9eef5;padding:3px 5px",
);
