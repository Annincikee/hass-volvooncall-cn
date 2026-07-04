const CARD_VERSION = "1.0.0";
const DEFAULT_IMAGE_URL = new URL(
  "./cartopview_complete_fallback.png",
  import.meta.url,
).href;

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
  connection: ["sensor", "connection_status"],
  engine_control: ["switch", "engine_remote_control"],
  climatization: ["switch", "climatization"],
  tailgate_control: ["switch", "tailgate_control"],
  sunroof_control: ["switch", "sunroof_control"],
  flash: ["button", "flash"],
  honk_flash: ["button", "honk_and_flash"],
};

const PARTS = [
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
  ["climatization", "switch", "空调", "mdi:air-conditioner"],
  ["engine_control", "switch", "远程启动", "mdi:engine-outline"],
  ["tailgate_control", "switch", "后备箱", "mdi:car-back"],
  ["flash", "button", "闪灯", "mdi:car-light-high"],
  ["honk_flash", "button", "鸣笛闪灯", "mdi:alarm-light-outline"],
];

const LABELS = {
  vin: "车辆 VIN",
  name: "卡片标题",
  model: "车型",
  image: "车辆俯视图 URL（留空使用本地素材）",
  show_controls: "显示远程控制",
  show_details: "显示详细状态",
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
        { name: "show_details", selector: { boolean: {} } },
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
      show_details: true,
    };
  }

  setConfig(config) {
    this._config = {
      name: "S90 T8",
      model: "s90_t8",
      show_controls: true,
      show_details: true,
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
    return this._config?.show_controls === false ? 7 : 9;
  }

  getGridOptions() {
    return {
      rows: this._config?.show_controls === false ? 7 : 9,
      columns: 12,
      min_rows: 6,
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
        return `${entityId || ""}:${state?.state || "missing"}`;
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
    if (!stateObj) return fallback;
    if (this._hass?.formatEntityState) {
      return this._hass.formatEntityState(stateObj);
    }
    const unit = stateObj.attributes?.unit_of_measurement;
    return `${stateObj.state}${unit ? ` ${unit}` : ""}`;
  }

  _openParts() {
    return PARTS.filter(([key]) => this._isOn(key));
  }

  _imageUrl() {
    const configured = String(this._config?.image || "").trim();
    if (!configured) return DEFAULT_IMAGE_URL;
    if (
      configured.startsWith("/") ||
      configured.startsWith("https://") ||
      configured.startsWith("http://")
    ) {
      return configured;
    }
    return DEFAULT_IMAGE_URL;
  }

  _escape(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  _metric(key, label, icon) {
    const unavailable = !this._isAvailable(key);
    return `
      <button class="metric ${unavailable ? "unavailable" : ""}"
              data-more-info="${this._escape(this._entityId(key) || "")}">
        <ha-icon icon="${icon}"></ha-icon>
        <span class="metric-copy">
          <span class="metric-value">${this._escape(this._displayState(key))}</span>
          <span class="metric-label">${label}</span>
        </span>
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

  _render() {
    if (!this.isConnected || !this._config) return;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });

    const vin = this._vin();
    if (!vin) {
      this.shadowRoot.innerHTML = `${this._styles()}
        <ha-card class="setup-card">
          <ha-icon icon="mdi:car-cog"></ha-icon>
          <div><strong>配置 Volvo 车辆卡片</strong><span>请在可视化编辑器中填写 VIN。</span></div>
        </ha-card>`;
      return;
    }

    const openParts = this._openParts();
    const isLocked = this._state("lock")?.state === "locked";
    const isOnline = this._isAvailable("connection")
      ? !["disconnected", "offline", "false"].includes(
          String(this._state("connection")?.state).toLowerCase(),
        )
      : true;
    const engineRunning = this._isOn("engine") || this._isOn("engine_control");
    const chargingState = String(this._state("charging_status")?.state || "").toLowerCase();
    const charging = this._isOn("charger_connection") || chargingState.includes("charg");
    const modelName = MODEL_LABELS[this._config.model] || this._config.model;
    const title = this._config.name || modelName || "Volvo";
    const summary = openParts.length
      ? `${openParts.length} 处未关闭`
      : isLocked
        ? "车辆已锁闭"
        : "车门车窗均已关闭";

    this.shadowRoot.innerHTML = `${this._styles()}
      <ha-card>
        <div class="header">
          <div class="identity">
            <span class="eyebrow">${this._escape(modelName || "VOLVO")}</span>
            <h2>${this._escape(title)}</h2>
            <button class="odometer" data-more-info="${this._escape(this._entityId("odometer"))}">
              ${this._escape(this._displayState("odometer"))}
            </button>
          </div>
          <div class="header-status">
            <span class="online ${isOnline ? "" : "offline"}">
              <span></span>${isOnline ? "已连接" : "离线"}
            </span>
            <button class="lock-button ${isLocked ? "locked" : "unlocked"}"
                    data-action="lock"
                    ${this._isAvailable("lock") ? "" : "disabled"}
                    aria-label="${isLocked ? "解锁车辆" : "锁定车辆"}">
              <ha-icon icon="${isLocked ? "mdi:lock" : "mdi:lock-open-variant"}"></ha-icon>
            </button>
          </div>
        </div>

        <div class="metrics">
          ${this._metric("fuel", "燃油", "mdi:gas-station")}
          ${this._metric("battery", "电量", "mdi:battery-charging")}
          ${this._metric("electric_range", "纯电续航", "mdi:ev-station")}
          ${this._metric("fuel_range", "综合余量", "mdi:map-marker-distance")}
        </div>

        <div class="vehicle-section">
          <div class="car-column">
            <div class="car-canvas ${openParts.length ? "has-warning" : ""}">
              <img src="${this._escape(this._imageUrl())}" alt="车辆俯视图" />
              <div class="asset-missing" hidden>
                <ha-icon icon="mdi:car-off"></ha-icon>
                <strong>车辆素材未安装</strong>
                <span>请运行 APK 提取脚本，或在卡片中设置 image。</span>
              </div>
              ${PARTS.map((part) => this._partOverlay(part)).join("")}
              <div class="center-status ${openParts.length ? "warning" : ""}">
                <ha-icon icon="${
                  openParts.length
                    ? "mdi:alert-circle"
                    : isLocked
                      ? "mdi:shield-lock"
                      : "mdi:check-circle"
                }"></ha-icon>
                <span>${summary}</span>
              </div>
            </div>
            <div class="vehicle-flags">
              ${engineRunning ? '<span class="flag running"><ha-icon icon="mdi:engine"></ha-icon>发动机运行中</span>' : ""}
              ${charging ? `<span class="flag charging"><ha-icon icon="mdi:lightning-bolt"></ha-icon>${this._escape(this._displayState("charging_status", "充电已连接"))}</span>` : ""}
            </div>
          </div>

          ${
            this._config.show_details === false
              ? ""
              : `<div class="details">
                  <div class="detail-heading"><span>车辆状态</span><small>点击项目查看详情</small></div>
                  <div class="detail-list">
                    ${this._detailRow("lock", "车辆锁", isLocked ? "已锁" : "未锁", isLocked ? "ok" : "warn")}
                    ${this._detailRow("window_lock", "车窗锁", this._displayState("window_lock"), this._state("window_lock")?.state === "locked" ? "ok" : "")}
                    ${this._detailRow("charging_status", "充电", this._displayState("charging_status"), charging ? "charge" : "")}
                    ${this._detailRow("full_charge_range", "最近满电续航", this._displayState("full_charge_range"), "charge")}
                    ${this._detailRow("charging_power", "充电功率", this._displayState("charging_power"), "")}
                    ${this._detailRow("charging_time", "预计充满", this._displayState("charging_time"), "")}
                  </div>
                  <div class="open-summary">
                    <span>开口状态</span>
                    <div>${
                      openParts.length
                        ? openParts
                            .map(
                              ([key, label]) =>
                                `<button data-more-info="${this._escape(this._entityId(key))}">${label}</button>`,
                            )
                            .join("")
                        : "<em>全部关闭</em>"
                    }</div>
                  </div>
                </div>`
          }
        </div>

        ${
          this._config.show_controls === false
            ? ""
            : `<div class="controls-wrap">
                <div class="section-title"><span>远程控制</span><small>关键操作需要确认</small></div>
                <div class="controls">${CONTROL_DEFINITIONS.map((control) => this._control(control)).join("")}</div>
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

  _detailRow(key, label, value, tone) {
    const entityId = this._entityId(key) || "";
    const available = this._isAvailable(key);
    return `<button class="detail-row ${tone} ${available ? "" : "missing"}"
                    data-more-info="${this._escape(entityId)}">
              <span>${label}</span><strong>${available ? this._escape(value) : "不可用"}</strong>
            </button>`;
  }

  _bindEvents() {
    const image = this.shadowRoot.querySelector(".car-canvas img");
    const missing = this.shadowRoot.querySelector(".asset-missing");
    image?.addEventListener("error", () => {
      image.hidden = true;
      if (missing) missing.hidden = false;
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
        --voc-line: color-mix(in srgb, var(--voc-text) 12%, transparent);
        --voc-blue: #1c6bba;
        --voc-warning: #bf2012;
        --voc-positive: #24724a;
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
        border-radius: var(--ha-card-border-radius, 18px);
      }
      .header {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 16px;
        padding: 22px 22px 12px;
      }
      .identity { min-width: 0; }
      .eyebrow {
        display: block;
        margin-bottom: 3px;
        color: var(--voc-secondary);
        font-size: 10px;
        font-weight: 600;
        letter-spacing: .13em;
        text-transform: uppercase;
      }
      h2 { margin: 0; font-size: 24px; font-weight: 400; line-height: 1.2; }
      .odometer {
        border: 0;
        padding: 4px 0 0;
        background: none;
        color: var(--voc-secondary);
        cursor: pointer;
        font-size: 12px;
      }
      .header-status { display: flex; align-items: center; gap: 10px; }
      .online {
        display: flex;
        align-items: center;
        gap: 6px;
        color: var(--voc-secondary);
        font-size: 11px;
        white-space: nowrap;
      }
      .online > span { width: 6px; height: 6px; border-radius: 50%; background: var(--voc-positive); }
      .online.offline > span { background: var(--voc-warning); }
      .lock-button {
        width: 42px;
        height: 42px;
        border: 0;
        border-radius: 50%;
        display: grid;
        place-items: center;
        background: var(--voc-surface);
        color: var(--voc-text);
        cursor: pointer;
      }
      .lock-button.locked { background: var(--voc-text); color: var(--voc-bg); }
      .lock-button:disabled { opacity: .35; cursor: default; }
      .metrics {
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        border-block: 1px solid var(--voc-line);
        margin-inline: 22px;
      }
      .metric {
        min-width: 0;
        min-height: 64px;
        border: 0;
        border-right: 1px solid var(--voc-line);
        padding: 11px 12px;
        display: flex;
        align-items: center;
        gap: 9px;
        background: transparent;
        color: var(--voc-text);
        text-align: left;
        cursor: pointer;
      }
      .metric:last-child { border-right: 0; }
      .metric ha-icon { --mdc-icon-size: 19px; color: var(--voc-blue); flex: 0 0 auto; }
      .metric-copy { display: flex; flex-direction: column; min-width: 0; }
      .metric-value { overflow: hidden; font-size: 15px; font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }
      .metric-label { color: var(--voc-secondary); font-size: 11px; white-space: nowrap; }
      .metric.unavailable { opacity: .42; }
      .vehicle-section {
        display: grid;
        grid-template-columns: minmax(200px, .9fr) minmax(180px, 1.1fr);
        gap: 8px;
        padding: 14px 22px 18px;
      }
      .car-column { min-width: 0; display: flex; flex-direction: column; align-items: center; }
      .car-canvas {
        position: relative;
        width: min(100%, 226px);
        aspect-ratio: 1248 / 2687;
        isolation: isolate;
      }
      .car-canvas img { width: 100%; height: 100%; object-fit: contain; display: block; filter: drop-shadow(0 13px 15px rgba(0,0,0,.12)); }
      .asset-missing {
        position: absolute;
        inset: 16% 5%;
        z-index: 5;
        border: 1px dashed var(--voc-line);
        border-radius: 18px;
        padding: 18px;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 7px;
        background: var(--voc-surface);
        color: var(--voc-secondary);
        text-align: center;
      }
      .asset-missing[hidden] { display: none; }
      .asset-missing ha-icon { --mdc-icon-size: 30px; color: var(--voc-blue); }
      .asset-missing strong { color: var(--voc-text); font-size: 12px; font-weight: 500; }
      .asset-missing span { font-size: 10px; line-height: 1.45; }
      .part {
        position: absolute;
        z-index: 2;
        border: 1px solid transparent;
        padding: 0;
        background: transparent;
        color: #fff;
        cursor: pointer;
      }
      .part.closed:focus-visible { border-color: var(--voc-blue); outline: 2px solid color-mix(in srgb, var(--voc-blue) 35%, transparent); }
      .part.open {
        border-color: color-mix(in srgb, var(--voc-warning) 85%, white);
        background: color-mix(in srgb, var(--voc-warning) 26%, transparent);
        box-shadow: 0 0 18px color-mix(in srgb, var(--voc-warning) 45%, transparent), inset 0 0 12px rgba(255,255,255,.18);
        animation: voc-pulse 2.2s ease-in-out infinite;
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
      .center-status {
        position: absolute;
        z-index: 4;
        left: 50%;
        top: 68%;
        display: flex;
        align-items: center;
        gap: 5px;
        padding: 6px 8px;
        border: 1px solid rgba(255,255,255,.24);
        border-radius: 11px;
        transform: translate(-50%, -50%);
        background: rgba(16,18,21,.79);
        box-shadow: 0 5px 16px rgba(0,0,0,.2);
        color: #fff;
        backdrop-filter: blur(8px);
        font-size: 9px;
        white-space: nowrap;
        pointer-events: none;
      }
      .center-status ha-icon { --mdc-icon-size: 13px; color: #74c69a; }
      .center-status.warning ha-icon { color: #ff766b; }
      .vehicle-flags { min-height: 24px; display: flex; justify-content: center; gap: 6px; flex-wrap: wrap; margin-top: 4px; }
      .flag { display: inline-flex; align-items: center; gap: 4px; padding: 4px 7px; border-radius: 9px; background: var(--voc-surface); color: var(--voc-secondary); font-size: 9px; }
      .flag ha-icon { --mdc-icon-size: 13px; }
      .flag.running { color: var(--voc-warning); }
      .flag.charging { color: var(--voc-blue); }
      .details { align-self: center; min-width: 0; padding: 8px 0 8px 10px; }
      .detail-heading, .section-title { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
      .detail-heading > span, .section-title > span { font-size: 13px; font-weight: 500; }
      .detail-heading small, .section-title small { color: var(--voc-secondary); font-size: 9px; }
      .detail-list { border-block: 1px solid var(--voc-line); }
      .detail-row {
        width: 100%;
        min-height: 34px;
        border: 0;
        border-bottom: 1px solid var(--voc-line);
        padding: 7px 2px;
        display: flex;
        justify-content: space-between;
        gap: 10px;
        background: transparent;
        color: var(--voc-secondary);
        cursor: pointer;
        font-size: 10px;
      }
      .detail-row:last-child { border-bottom: 0; }
      .detail-row strong { overflow: hidden; color: var(--voc-text); font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }
      .detail-row.warn strong { color: var(--voc-warning); }
      .detail-row.charge strong { color: var(--voc-blue); }
      .detail-row.missing { opacity: .4; cursor: default; }
      .open-summary { margin-top: 12px; }
      .open-summary > span { display: block; margin-bottom: 6px; color: var(--voc-secondary); font-size: 9px; }
      .open-summary > div { display: flex; flex-wrap: wrap; gap: 5px; }
      .open-summary button, .open-summary em { border: 0; border-radius: 8px; padding: 4px 7px; background: color-mix(in srgb, var(--voc-warning) 11%, transparent); color: var(--voc-warning); font-size: 9px; font-style: normal; }
      .open-summary button { cursor: pointer; }
      .open-summary em { background: var(--voc-surface); color: var(--voc-positive); }
      .controls-wrap { border-top: 1px solid var(--voc-line); padding: 14px 22px 20px; }
      .controls { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 7px; }
      .control {
        min-width: 0;
        border: 0;
        border-radius: 12px;
        padding: 9px 4px 8px;
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 5px;
        background: var(--voc-surface);
        color: var(--voc-secondary);
        cursor: pointer;
        font-size: 9px;
      }
      .control-icon { width: 30px; height: 30px; display: grid; place-items: center; border-radius: 50%; background: var(--voc-bg); color: var(--voc-text); }
      .control-icon ha-icon { --mdc-icon-size: 17px; }
      .control.active { color: var(--voc-blue); }
      .control.active .control-icon { background: var(--voc-blue); color: #fff; }
      .control:disabled { opacity: .35; cursor: default; }
      .confirm-dialog {
        width: min(88vw, 330px);
        border: 0;
        border-radius: 20px;
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
      .dialog-actions button { min-height: 42px; border: 0; border-radius: 11px; cursor: pointer; }
      .dialog-cancel { background: var(--voc-surface); color: var(--voc-text); }
      .dialog-confirm { background: var(--voc-blue); color: #fff; }
      .error { margin: -8px 22px 16px; border-radius: 9px; padding: 8px 10px; background: color-mix(in srgb, var(--voc-warning) 12%, transparent); color: var(--voc-warning); font-size: 11px; }
      .setup-card { min-height: 130px; padding: 24px; display: flex; align-items: center; gap: 14px; }
      .setup-card > ha-icon { --mdc-icon-size: 34px; color: var(--voc-blue); }
      .setup-card div { display: flex; flex-direction: column; gap: 4px; }
      .setup-card strong { font-weight: 500; }
      .setup-card span { color: var(--voc-secondary); font-size: 12px; }
      button:focus-visible { outline: 2px solid var(--voc-blue); outline-offset: 2px; }
      @keyframes voc-pulse { 0%, 100% { opacity: .84; } 50% { opacity: 1; } }
      @media (max-width: 520px) {
        .header { padding: 18px 16px 10px; }
        h2 { font-size: 21px; }
        .online { display: none; }
        .metrics { margin-inline: 16px; grid-template-columns: repeat(2, minmax(0, 1fr)); }
        .metric:nth-child(2) { border-right: 0; }
        .metric:nth-child(-n+2) { border-bottom: 1px solid var(--voc-line); }
        .vehicle-section { grid-template-columns: minmax(0, 1fr) minmax(128px, .78fr); padding: 12px 12px 16px; gap: 2px; }
        .car-canvas { width: min(100%, 205px); }
        .details { padding-left: 4px; }
        .detail-heading small { display: none; }
        .controls-wrap { padding: 12px 16px 17px; }
        .controls { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      }
      @media (max-width: 360px) {
        .vehicle-section { grid-template-columns: minmax(0, 1fr); }
        .car-canvas { width: 190px; }
        .details { width: 100%; padding: 0 4px; }
        .detail-list { display: grid; grid-template-columns: 1fr 1fr; }
        .detail-row:nth-child(odd) { padding-right: 8px; }
        .detail-row:nth-child(even) { padding-left: 8px; }
      }
      @media (prefers-reduced-motion: reduce) { .part.open { animation: none; } }
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
    name: "Volvo 车辆控制卡",
    description: "移动端优先的车辆状态与远程控制卡片，优先适配 S90 T8。",
    preview: true,
    documentationURL: "https://github.com/Annincikee/hass-volvooncall-cn",
    getEntitySuggestion: (_hass, entityId) => {
      const match = entityId.match(
        /^(?:lock|sensor|binary_sensor|switch)\.([a-z0-9]+)_(?:lock|engine|battery_charge_level|full_charge_electric_range|fuel_amount)$/,
      );
      if (!match) return null;
      return {
        config: {
          type: "custom:volvo-car-card",
          vin: match[1].toUpperCase(),
          name: "S90 T8",
          model: "s90_t8",
          show_controls: true,
          show_details: true,
        },
      };
    },
  });
}

console.info(`%c VOLVO-CAR-CARD %c v${CARD_VERSION} `, "color:#fff;background:#284e80;padding:3px 5px", "color:#284e80;background:#e9eef5;padding:3px 5px");
