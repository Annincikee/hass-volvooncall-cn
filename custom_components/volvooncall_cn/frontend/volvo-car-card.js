const CARD_VERSION = "2.2.0";

const MODEL_ASSETS = {
  s90: new URL("./assets/car-s90-black-card.webp", import.meta.url).href,
  xc60: new URL("./assets/car-xc60-black-card.webp", import.meta.url).href,
  xc90: new URL("./assets/car-xc90-black-card.webp", import.meta.url).href,
};

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
  ["climatization", "switch", "温度调节", "mdi:air-conditioner"],
  ["engine_control", "switch", "远程启动", "mdi:engine-outline"],
  ["tailgate_control", "switch", "后备箱", "mdi:car-back"],
  ["sunroof_control", "switch", "天窗", "mdi:home-roof"],
  ["flash", "button", "闪灯", "mdi:car-light-high"],
  ["honk_flash", "button", "鸣笛闪灯", "mdi:alarm-light-outline"],
];

const LABELS = {
  vin: "车辆 VIN",
  name: "卡片标题",
  model: "车型",
  image: "车辆俯视图 URL（可选，留空使用黑色内置车模）",
  show_controls: "显示远程控制",
  show_statistics: "显示行程统计",
};

const MODEL_LABELS = {
  s90_t8: "S90 Recharge T8",
  s90: "S90",
  xc60_t8: "XC60 Recharge T8",
  xc60: "XC60",
  xc90_t8: "XC90 Recharge T8",
  xc90: "XC90",
  generic: "Volvo",
};

const MODEL_FAMILIES = {
  s90_t8: "s90",
  s90: "s90",
  xc60_t8: "xc60",
  xc60: "xc60",
  xc90_t8: "xc90",
  xc90: "xc90",
  generic: "xc60",
};

class VolvoCarCard extends HTMLElement {
  constructor() {
    super();
    this._pendingActions = new Set();
    this._feedbackTimer = undefined;
    this._hasRenderedVehicle = false;
  }

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
                { value: "xc60", label: "XC60" },
                { value: "xc90_t8", label: "XC90 T8" },
                { value: "xc90", label: "XC90" },
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
    this._hasRenderedVehicle = false;
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

  _isControlAvailable(key) {
    const stateObj = this._state(key);
    if (!stateObj || stateObj.state === "unavailable") return false;
    if (stateObj.entity_id?.startsWith("button.")) return true;
    return stateObj.state !== "unknown";
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
    if (configured.startsWith("/") || configured.startsWith("https://")) {
      return configured;
    }
    return MODEL_ASSETS[this._modelFamily()] || MODEL_ASSETS.xc60;
  }

  _modelFamily() {
    return MODEL_FAMILIES[this._config?.model] || "xc60";
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
    const lockPending = this._pendingActions.has("lock");
    const connection = String(this._state("connection")?.state || "").toLowerCase();
    const isOnline = !["disconnected", "offline", "false"].includes(connection);
    const charging = this._isCharging();
    const imageUrl = this._imageUrl();
    const modelFamily = this._modelFamily();
    const hasElectric = this._isAvailable("electric_range") || this._isAvailable("battery");
    const hasFuel = this._isAvailable("fuel_range") || this._isAvailable("fuel");
    const rangeTiles = [
      hasElectric
        ? this._rangeTile("electric_range", "纯电续航", "battery", "electric")
        : "",
      hasFuel ? this._rangeTile("fuel_range", "燃油续航", "fuel", "fuel") : "",
    ].filter(Boolean);
    const animateIn = !this._hasRenderedVehicle && Boolean(this._hass);

    this.shadowRoot.innerHTML = `${this._styles()}
      <ha-card class="${animateIn ? "animate-in" : ""}">
        <div class="hero">
          <div class="identity">
            <span class="eyebrow">CONNECTED VEHICLE</span>
            <h2>${this._escape(title)}</h2>
            <div class="hero-meta">
              <span class="connection ${isOnline ? "" : "offline"}">
                <i></i>${isOnline ? "车辆在线" : "车辆离线"}
              </span>
              <button class="link-value" data-more-info="${this._escape(this._entityId("odometer"))}">
                <ha-icon icon="mdi:road-variant"></ha-icon>${this._escape(this._displayState("odometer"))}
              </button>
            </div>
          </div>
          <button class="lock-pill ${isLocked ? "locked" : "unlocked"} ${lockPending ? "pending" : ""}"
                  data-action="lock"
                  ${this._isControlAvailable("lock") && !lockPending ? "" : "disabled"}
                  aria-busy="${lockPending}"
                  aria-label="${isLocked ? "解锁车辆" : "锁定车辆"}">
            <ha-icon class="${lockPending ? "pending-icon" : ""}" icon="${lockPending ? "mdi:loading" : isLocked ? "mdi:lock" : "mdi:lock-open-variant"}"></ha-icon>
            <span>${lockPending ? "发送中" : isLocked ? "已锁定" : "未锁定"}</span>
          </button>
        </div>

        <div class="range-band ${rangeTiles.length === 1 ? "single" : ""}">
          ${rangeTiles.join("") || `<div class="range-empty">续航数据同步中</div>`}
        </div>

        <div class="vehicle-area">
          <div class="vehicle-visual">
            <div class="car-canvas model-${modelFamily} ${openParts.length ? "has-warning" : ""}">
              <img src="${this._escape(imageUrl)}" alt="${this._escape(modelName)} 黑色车辆俯视图" />
              ${BODY_PARTS.map((part) => this._partOverlay(part, openParts.length <= 2)).join("")}
            </div>
            <div class="vehicle-summary ${openParts.length ? "warning" : "ok"}">
              <ha-icon icon="${openParts.length ? "mdi:alert-circle" : isLocked ? "mdi:shield-lock" : "mdi:check-circle"}"></ha-icon>
              <span>${this._escape(this._vehicleSummary(isLocked, openParts))}</span>
            </div>
          </div>
          <div class="state-panel">
            <div class="state-heading">
              <span>车辆状态</span>
              <small class="${openParts.length ? "warn" : "ok"}">${openParts.length ? "请检查" : "状态正常"}</small>
            </div>
            ${this._stateRow("engine", "发动机", this._isOn("engine") ? "运行中" : "关闭", this._isOn("engine") ? "warn" : "", "mdi:engine-outline")}
            ${
              this._isAvailable("charging_status")
                ? this._stateRow("charging_status", "充电状态", this._displayState("charging_status"), charging ? "charge" : "", "mdi:battery-charging")
                : ""
            }
            ${
              this._isAvailable("full_charge_range")
                ? this._stateRow("full_charge_range", "最近满电", this._displayState("full_charge_range"), "charge", "mdi:map-marker-distance")
                : ""
            }
            <div class="open-list">
              <span><b>门窗与舱盖</b><small>${openParts.length ? `${openParts.length} 项需检查` : "全部关闭"}</small></span>
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
                  <div><ha-icon icon="mdi:chart-timeline-variant"></ha-icon><span>行程统计</span></div>
                  <small>TM 手动复位 · TA 自动复位</small>
                </div>
                <div class="trip-table">
                  <div class="trip-labels"><span></span><span>里程</span><span>均速</span><span>油耗</span><span>电耗</span></div>
                  ${this._tripRow("TM", [
                    ["tm_distance", "里程"],
                    ["tm_average_speed", "均速"],
                    ["tm_fuel_consumption", "油耗"],
                    ["tm_energy_consumption", "电耗"],
                  ])}
                  ${this._tripRow("TA", [
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
                  <div><ha-icon icon="mdi:car-key"></ha-icon><span>远程控制</span></div>
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
        <div class="feedback" role="status" aria-live="polite" hidden>
          <ha-icon icon="mdi:check-circle"></ha-icon><span></span>
        </div>
      </ha-card>`;

    this._bindEvents();
    if (this._hass) this._hasRenderedVehicle = true;
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
    return isLocked ? "车辆安全锁闭" : "门窗均已关闭";
  }

  _rangeTile(key, label, levelKey, tone) {
    const value = this._displayState(key);
    const level = this._stateNumber(levelKey);
    const percentage = Number.isFinite(level) ? Math.max(0, Math.min(100, level)) : 0;
    const levelText = this._displayState(levelKey);
    const icon = tone === "electric" ? "mdi:lightning-bolt" : "mdi:gas-station";
    return `
      <button class="range-tile ${tone}" data-more-info="${this._escape(this._entityId(key))}">
        <span class="range-top"><span><ha-icon icon="${icon}"></ha-icon>${label}</span><small>${this._escape(levelText)}</small></span>
        <strong>${this._escape(value)}</strong>
        <span class="level-track" aria-hidden="true"><i style="width:${percentage}%"></i></span>
      </button>`;
  }

  _partOverlay(part, showLabel = true) {
    const [key, label, className] = part;
    const isOpen = this._isOn(key);
    const entityId = this._entityId(key) || "";
    return `
      <button class="part ${className} ${isOpen ? "open" : "closed"}"
              aria-label="${label}${isOpen ? "已打开" : "已关闭"}"
              title="${label} · ${isOpen ? "已打开" : "已关闭"}"
              data-more-info="${this._escape(entityId)}">
        <span class="part-dot"></span>
        ${isOpen && showLabel ? `<span class="part-label">${label}</span>` : ""}
      </button>`;
  }

  _stateRow(key, label, value, tone, icon) {
    return `<button class="state-row ${tone || ""} ${this._isAvailable(key) ? "" : "missing"}"
                    data-more-info="${this._escape(this._entityId(key))}">
              <span class="row-label">${icon ? `<ha-icon icon="${icon}"></ha-icon>` : ""}<span>${label}</span></span>
              <strong>${this._escape(value)}</strong>
            </button>`;
  }

  _tripRow(title, rows) {
    return `
      <div class="trip-row">
        <strong class="trip-title">${title}</strong>
        ${rows
          .map(([key, label]) => {
            const state = this._state(key);
            const available = this._isAvailable(key);
            const value = available ? state.state : "—";
            const unit = available ? state.attributes?.unit_of_measurement || "" : "";
            return `
              <button data-more-info="${this._escape(this._entityId(key))}" aria-label="${title} ${label} ${this._escape(this._displayState(key))}">
                <strong>${this._escape(value)}</strong>
                <span>${this._escape(unit)}</span>
              </button>`;
          })
          .join("")}
        ${rows.length < 4 ? `<span class="trip-na" aria-label="TA 电耗无原始数据">—</span>` : ""}
      </div>`;
  }

  _control(control) {
    const [key, kind, label, icon] = control;
    const stateObj = this._state(key);
    const available = this._isControlAvailable(key);
    const pending = this._pendingActions.has(key);
    const active =
      kind === "lock" ? stateObj?.state === "locked" : stateObj?.state === "on";
    let dynamicLabel = label;
    if (kind === "lock") dynamicLabel = active ? "已锁车" : "未锁车";
    if (key === "engine_control" && active) dynamicLabel = "停止发动机";
    if (key === "climatization" && active) dynamicLabel = "关闭空调";
    if (key === "tailgate_control" && active) dynamicLabel = "关闭后备箱";
    if (key === "sunroof_control" && active) dynamicLabel = "关闭天窗";
    return `
      <button class="control ${active ? "active" : ""} ${pending ? "pending" : ""}"
              data-action="${key}"
              ${available && !pending ? "" : "disabled"}
              aria-busy="${pending}"
              aria-label="${dynamicLabel}">
        <span class="control-icon"><ha-icon class="${pending ? "pending-icon" : ""}" icon="${pending ? "mdi:loading" : icon}"></ha-icon></span>
        <span>${pending ? "发送中" : dynamicLabel}</span>
      </button>`;
  }

  _bindEvents() {
    const image = this.shadowRoot.querySelector(".car-canvas img");
    image?.addEventListener("error", () => {
      const fallbackUrl = MODEL_ASSETS[this._modelFamily()] || MODEL_ASSETS.xc60;
      if (!image.dataset.fallbackTried && image.src !== fallbackUrl) {
        image.dataset.fallbackTried = "true";
        image.src = fallbackUrl;
        image.alt = `${MODEL_LABELS[this._config.model] || "Volvo"} 黑色车辆俯视图`;
        return;
      }
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
    if (this._pendingActions.has(key)) return;
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
    this._pendingActions.add(key);
    this._render();
    let feedbackMessage = "指令已发送";
    let feedbackTone = "success";
    try {
      await this._hass.callService(domain, service, { entity_id: entityId });
      feedbackMessage = this._actionSuccessMessage(key, service);
    } catch (error) {
      feedbackMessage = `操作失败：${error?.message || error}`;
      feedbackTone = "error";
    } finally {
      this._pendingActions.delete(key);
      this._render();
      this._showFeedback(feedbackMessage, feedbackTone);
    }
  }

  _actionSuccessMessage(key, service) {
    if (key === "lock") return service === "unlock" ? "解锁指令已发送" : "锁车指令已发送";
    if (key === "engine_control") return service === "turn_on" ? "远程启动指令已发送" : "停止发动机指令已发送";
    if (key === "climatization") return service === "turn_on" ? "温度调节已开启" : "温度调节已关闭";
    if (key === "tailgate_control") return "后备箱指令已发送";
    if (key === "sunroof_control") return "天窗指令已发送";
    if (key === "flash") return "闪灯指令已发送";
    if (key === "honk_flash") return "鸣笛闪灯指令已发送";
    return "指令已发送";
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

  _showFeedback(message, tone = "success") {
    const element = this.shadowRoot?.querySelector(".feedback");
    if (!element) return;
    if (this._feedbackTimer) clearTimeout(this._feedbackTimer);
    element.hidden = !message;
    element.classList.toggle("error", tone === "error");
    const icon = element.querySelector("ha-icon");
    if (icon) icon.setAttribute("icon", tone === "error" ? "mdi:alert-circle" : "mdi:check-circle");
    const text = element.querySelector("span");
    if (text) text.textContent = message;
    if (!message) return;
    this._feedbackTimer = setTimeout(() => {
      element.hidden = true;
      this._feedbackTimer = undefined;
    }, 3200);
    this._feedbackTimer?.unref?.();
  }

  _styles() {
    return `<style>
      :host {
        --voc-text: var(--primary-text-color, #141414);
        --voc-secondary: var(--secondary-text-color, #707070);
        --voc-bg: var(--ha-card-background, var(--card-background-color, #fff));
        --voc-panel: color-mix(in srgb, var(--voc-bg) 97%, var(--voc-text));
        --voc-surface: color-mix(in srgb, var(--voc-bg) 95%, var(--voc-text));
        --voc-surface-strong: color-mix(in srgb, var(--voc-bg) 90%, var(--voc-text));
        --voc-line: color-mix(in srgb, var(--voc-text) 13%, transparent);
        --voc-line-soft: color-mix(in srgb, var(--voc-text) 8%, transparent);
        --voc-blue: #1c6eba;
        --voc-warning: #cd2314;
        --voc-orange: #eb7400;
        --voc-positive: #04721c;
        --voc-accent: color-mix(in srgb, var(--voc-blue) 74%, var(--voc-text));
        --voc-danger: color-mix(in srgb, var(--voc-warning) 72%, var(--voc-text));
        --voc-success: color-mix(in srgb, var(--voc-positive) 72%, var(--voc-text));
        display: block;
        container-type: inline-size;
        color: var(--voc-text);
        font-family: var(--ha-font-family-body, "Helvetica Neue", Arial, sans-serif);
      }
      * { box-sizing: border-box; }
      button { font: inherit; -webkit-tap-highlight-color: transparent; }
      ha-card {
        position: relative;
        display: block;
        overflow: hidden;
        container-type: inline-size;
        background: var(--voc-bg);
        border: 1px solid var(--voc-line-soft);
        border-radius: var(--ha-card-border-radius, 18px);
        box-shadow: 0 12px 32px rgba(0, 0, 0, .08);
      }
      .hero {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 18px;
        padding: 22px 22px 16px;
      }
      .identity { min-width: 0; }
      .eyebrow {
        display: block;
        margin-bottom: 5px;
        color: var(--voc-secondary);
        font-size: 9px;
        font-weight: 600;
        letter-spacing: .18em;
        text-transform: uppercase;
      }
      h2 {
        margin: 0;
        overflow: hidden;
        font-size: 27px;
        font-weight: 450;
        letter-spacing: -.025em;
        line-height: 1.08;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .hero-meta {
        min-height: 20px;
        padding-top: 7px;
        display: flex;
        align-items: center;
        gap: 10px;
        color: var(--voc-secondary);
        font-size: 10px;
      }
      .connection { display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }
      .connection i { width: 6px; height: 6px; border-radius: 50%; background: var(--voc-positive); }
      .connection.offline i { background: var(--voc-warning); }
      .hero-meta::after { width: 1px; height: 11px; background: var(--voc-line); content: ""; order: 1; }
      .link-value {
        min-width: 0;
        border: 0;
        padding: 0;
        display: inline-flex;
        align-items: center;
        gap: 4px;
        order: 2;
        background: none;
        color: inherit;
        cursor: pointer;
        font-size: 10px;
        white-space: nowrap;
      }
      .link-value ha-icon { --mdc-icon-size: 13px; }
      .lock-pill {
        min-width: 94px;
        min-height: 46px;
        border: 1px solid var(--voc-line);
        border-radius: 24px;
        padding: 0 15px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        gap: 7px;
        background: var(--voc-surface);
        color: var(--voc-text);
        cursor: pointer;
        flex: 0 0 auto;
        font-size: 11px;
        font-weight: 550;
        transition: transform .18s ease, box-shadow .18s ease, background-color .18s ease, color .18s ease;
      }
      .lock-pill ha-icon { --mdc-icon-size: 18px; }
      .lock-pill.locked { border-color: var(--voc-text); background: var(--voc-text); color: var(--voc-bg); }
      .lock-pill.unlocked { border-color: color-mix(in srgb, var(--voc-warning) 55%, transparent); color: var(--voc-danger); }
      .lock-pill:disabled { opacity: .4; cursor: default; }
      .lock-pill.pending:disabled { opacity: .78; }
      .range-band {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 10px;
        margin: 0 22px;
      }
      .range-band.single { grid-template-columns: 1fr; }
      .range-tile {
        min-width: 0;
        min-height: 102px;
        border: 1px solid var(--voc-line-soft);
        border-radius: 14px;
        padding: 13px 15px 14px;
        display: flex;
        flex-direction: column;
        align-items: stretch;
        justify-content: space-between;
        gap: 7px;
        background: var(--voc-surface);
        color: var(--voc-text);
        text-align: left;
        cursor: pointer;
        box-shadow: 0 8px 18px rgba(0, 0, 0, .035);
        transition: transform .18s ease, border-color .18s ease, box-shadow .18s ease;
      }
      .range-top {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        color: var(--voc-secondary);
        font-size: 10px;
      }
      .range-top > span { display: inline-flex; align-items: center; gap: 5px; }
      .range-top ha-icon { --mdc-icon-size: 15px; color: var(--voc-blue); }
      .range-top small { overflow: hidden; font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
      .range-tile.fuel .range-top ha-icon { color: var(--voc-orange); }
      .range-tile > strong {
        overflow: hidden;
        font-size: 25px;
        font-weight: 450;
        letter-spacing: -.02em;
        line-height: 1.1;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .level-track {
        height: 4px;
        overflow: hidden;
        border-radius: 999px;
        background: color-mix(in srgb, var(--voc-text) 12%, transparent);
      }
      .level-track i {
        display: block;
        height: 100%;
        border-radius: inherit;
        background: var(--voc-blue);
        transform-origin: left center;
      }
      .range-tile.fuel .level-track i { background: var(--voc-orange); }
      .range-empty { min-height: 72px; border-radius: 14px; display: grid; place-items: center; background: var(--voc-surface); color: var(--voc-secondary); font-size: 11px; }
      .vehicle-area {
        display: grid;
        grid-template-columns: minmax(184px, 1.08fr) minmax(156px, .92fr);
        gap: 18px;
        margin: 16px 22px 18px;
        border: 1px solid var(--voc-line-soft);
        border-radius: 18px;
        padding: 17px 16px 16px;
        background: var(--voc-panel);
        box-shadow: inset 0 1px 0 color-mix(in srgb, var(--voc-bg) 72%, transparent), 0 12px 28px rgba(0, 0, 0, .04);
      }
      .vehicle-visual { min-width: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; }
      .car-canvas {
        position: relative;
        width: min(100%, 208px);
        aspect-ratio: 700 / 1500;
        isolation: isolate;
        --hood-top: 5%;
        --front-row: 32%;
        --rear-row: 52.5%;
        --tail-top: 80.5%;
      }
      .car-canvas.model-s90 { --hood-top: 4%; --front-row: 32%; --rear-row: 52.5%; --tail-top: 81%; }
      .car-canvas.model-xc90 { --hood-top: 5%; --front-row: 32%; --rear-row: 52.5%; --tail-top: 80%; }
      .car-canvas img {
        position: relative;
        z-index: 1;
        width: 100%;
        height: 100%;
        object-fit: contain;
        display: block;
        filter: drop-shadow(0 14px 13px rgba(0,0,0,.22));
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
        border-color: transparent;
        background: transparent;
        box-shadow: none;
        animation: voc-warning-in .34s ease-out both;
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
        padding: 4px 7px;
        border-radius: 9px;
        background: var(--voc-warning);
        box-shadow: 0 2px 7px rgba(0,0,0,.2);
        font-size: 9px;
        font-weight: 600;
        pointer-events: none;
        white-space: nowrap;
      }
      .hood { left: 11%; top: var(--hood-top); width: 78%; height: 26%; clip-path: polygon(12% 5%, 88% 5%, 100% 88%, 0 88%); }
      .hood .part-dot { right: 45%; bottom: 8%; }
      .hood .part-label { left: 50%; bottom: 8%; transform: translate(-50%, 0); }
      .door { width: 38%; height: 21%; }
      .door.fl { left: 5%; top: var(--front-row); clip-path: polygon(20% 0, 100% 8%, 94% 100%, 2% 92%); }
      .door.fr { right: 5%; top: var(--front-row); clip-path: polygon(0 8%, 80% 0, 98% 92%, 6% 100%); }
      .door.rl { left: 5%; top: var(--rear-row); clip-path: polygon(2% 8%, 94% 0, 100% 92%, 20% 100%); }
      .door.rr { right: 5%; top: var(--rear-row); clip-path: polygon(6% 0, 98% 8%, 80% 100%, 0 92%); }
      .door.fl .part-dot, .door.rl .part-dot { left: 5%; top: 43%; }
      .door.fr .part-dot, .door.rr .part-dot { right: 5%; top: 43%; }
      .door .part-label { left: 50%; top: 76%; transform: translate(-50%, -50%); }
      .window { width: 16%; height: 20%; border-radius: 40%; }
      .window.wfl { left: 24.5%; top: calc(var(--front-row) - 1%); }
      .window.wfr { right: 24.5%; top: calc(var(--front-row) - 1%); }
      .window.wrl { left: 24.5%; top: calc(var(--rear-row) - 1%); }
      .window.wrr { right: 24.5%; top: calc(var(--rear-row) - 1%); }
      .window .part-dot { left: 50%; top: 50%; transform: translate(-50%, -50%); }
      .window .part-label { left: 50%; top: 30%; transform: translate(-50%, -50%); }
      .sunroof { left: 30%; top: 40.5%; width: 40%; height: 29%; border-radius: 32% 32% 22% 22%; }
      .sunroof .part-dot { left: calc(50% - 4px); top: 10%; }
      .sunroof .part-label { left: 50%; top: 50%; transform: translate(-50%, -50%); }
      .tailgate { left: 14%; top: var(--tail-top); width: 72%; height: 14%; clip-path: polygon(0 10%, 100% 10%, 90% 94%, 10% 94%); }
      .tailgate .part-dot { left: calc(50% - 4px); top: 8%; }
      .tailgate .part-label { left: 50%; top: 0; transform: translate(-50%, -95%); }
      .vehicle-summary {
        min-height: 30px;
        margin-top: -7px;
        border-radius: 15px;
        padding: 0 11px;
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 5px;
        background: var(--voc-surface);
        border: 1px solid var(--voc-line-soft);
        color: var(--voc-secondary);
        font-size: 10px;
        white-space: nowrap;
      }
      .vehicle-summary ha-icon { --mdc-icon-size: 14px; color: var(--voc-success); }
      .vehicle-summary.warning { background: color-mix(in srgb, var(--voc-warning) 10%, var(--voc-bg)); color: var(--voc-danger); }
      .vehicle-summary.warning ha-icon { color: var(--voc-danger); }
      .state-panel {
        align-self: center;
        min-width: 0;
        border-top: 1px solid var(--voc-line);
      }
      .state-heading {
        min-height: 42px;
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        border-bottom: 1px solid var(--voc-line);
      }
      .state-heading > span { font-size: 11px; font-weight: 600; }
      .state-heading small { font-size: 9px; }
      .state-heading small.ok { color: var(--voc-success); }
      .state-heading small.warn { color: var(--voc-danger); }
      .state-heading small::before { display: inline-block; width: 5px; height: 5px; margin-right: 4px; border-radius: 50%; background: currentColor; content: ""; vertical-align: 1px; }
      .state-row {
        width: 100%;
        min-height: 40px;
        border: 0;
        border-bottom: 1px solid var(--voc-line);
        padding: 8px 0;
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 10px;
        background: transparent;
        color: var(--voc-secondary);
        cursor: pointer;
        font-size: 10px;
        transition: background-color .16s ease, color .16s ease;
      }
      .row-label { min-width: 0; display: inline-flex; align-items: center; gap: 6px; }
      .row-label ha-icon { --mdc-icon-size: 14px; color: var(--voc-secondary); }
      .state-row strong {
        overflow: hidden;
        color: var(--voc-text);
        font-size: 10px;
        font-weight: 600;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .state-row.warn strong { color: var(--voc-danger); }
      .state-row.warn .row-label ha-icon { color: var(--voc-danger); }
      .state-row.charge strong { color: var(--voc-accent); }
      .state-row.charge .row-label ha-icon { color: var(--voc-accent); }
      .state-row.missing { opacity: .45; cursor: default; }
      .open-list { padding: 11px 0 4px; }
      .open-list > span { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; margin-bottom: 7px; color: var(--voc-secondary); font-size: 9px; }
      .open-list > span b { color: var(--voc-text); font-size: 10px; font-weight: 600; }
      .open-list > span small { color: var(--voc-danger); font-size: 9px; white-space: nowrap; }
      .open-list > div { display: flex; flex-wrap: wrap; gap: 5px; }
      .open-list button,
      .open-list em {
        border: 0;
        border-radius: 8px;
        padding: 4px 7px;
        background: color-mix(in srgb, var(--voc-warning) 14%, var(--voc-bg));
        color: var(--voc-danger);
        font-size: 10px;
        font-style: normal;
      }
      .open-list em { background: var(--voc-surface); color: var(--voc-success); }
      .statistics { border-top: 1px solid var(--voc-line); padding: 15px 22px 17px; }
      .section-title {
        display: flex;
        align-items: baseline;
        justify-content: space-between;
        gap: 10px;
        margin-bottom: 11px;
      }
      .section-title > div { display: flex; align-items: center; gap: 7px; }
      .section-title > div ha-icon { --mdc-icon-size: 16px; color: var(--voc-secondary); }
      .section-title > div span { font-size: 12px; font-weight: 600; }
      .section-title small { color: var(--voc-secondary); font-size: 10px; }
      .trip-table {
        overflow: hidden;
        border: 1px solid var(--voc-line);
        border-radius: 13px;
        background: var(--voc-surface);
      }
      .trip-labels,
      .trip-row { display: grid; grid-template-columns: 38px repeat(4, minmax(0, 1fr)); align-items: stretch; }
      .trip-labels { min-height: 29px; align-items: center; color: var(--voc-secondary); font-size: 9px; text-align: center; }
      .trip-row { min-height: 59px; border-top: 1px solid var(--voc-line); }
      .trip-title { display: grid; place-items: center; color: var(--voc-secondary); font-size: 10px; letter-spacing: .08em; }
      .trip-row button {
        min-width: 0;
        border: 0;
        border-left: 1px solid var(--voc-line);
        padding: 8px 5px;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 2px;
        background: transparent;
        color: var(--voc-text);
        text-align: center;
        cursor: pointer;
        transition: background-color .16s ease;
      }
      .trip-row button strong { width: 100%; overflow: hidden; font-size: 11px; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
      .trip-row button span { width: 100%; overflow: hidden; color: var(--voc-secondary); font-size: 8px; line-height: 1.1; text-overflow: ellipsis; white-space: nowrap; }
      .trip-na { border-left: 1px solid var(--voc-line); display: grid; place-items: center; color: var(--voc-secondary); font-size: 12px; }
      .controls-wrap { border-top: 1px solid var(--voc-line); padding: 15px 22px 21px; }
      .controls { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 7px; }
      .control {
        min-width: 0;
        min-height: 70px;
        border: 1px solid transparent;
        border-radius: 12px;
        padding: 9px 3px 8px;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 6px;
        background: var(--voc-surface);
        color: var(--voc-secondary);
        cursor: pointer;
        font-size: 9px;
        box-shadow: 0 6px 14px rgba(0, 0, 0, .025);
        transition: transform .18s ease, border-color .18s ease, background-color .18s ease, box-shadow .18s ease, color .18s ease;
      }
      .control-icon {
        width: 34px;
        height: 34px;
        display: grid;
        place-items: center;
        border-radius: 50%;
        background: var(--voc-bg);
        color: var(--voc-text);
        box-shadow: inset 0 0 0 1px var(--voc-line-soft);
        transition: transform .18s ease, background-color .18s ease, color .18s ease;
      }
      .control-icon ha-icon { --mdc-icon-size: 18px; }
      .control.active { border-color: color-mix(in srgb, var(--voc-blue) 28%, transparent); background: color-mix(in srgb, var(--voc-blue) 8%, var(--voc-bg)); color: var(--voc-accent); }
      .control.active .control-icon { background: var(--voc-blue); color: #fff; }
      .control[data-action="engine_control"].active { border-color: color-mix(in srgb, var(--voc-orange) 35%, transparent); background: color-mix(in srgb, var(--voc-orange) 8%, var(--voc-bg)); color: var(--voc-orange); }
      .control[data-action="engine_control"].active .control-icon { background: var(--voc-orange); }
      .control:disabled { opacity: .35; cursor: default; }
      .control.pending:disabled { opacity: .76; }
      .pending-icon { animation: voc-spin .8s linear infinite; }
      .animate-in .level-track i { animation: voc-progress-in .55s cubic-bezier(.2, .7, .2, 1) both; }
      .animate-in .car-canvas img { animation: voc-car-in .55s cubic-bezier(.2, .75, .2, 1) both; }
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
      .confirm-dialog[open] { animation: voc-dialog-in .2s cubic-bezier(.2, .8, .2, 1) both; }
      .confirm-dialog::backdrop { background: rgba(0,0,0,.48); backdrop-filter: blur(3px); }
      .dialog-icon { width: 46px; height: 46px; margin: 0 auto 13px; border-radius: 50%; display: grid; place-items: center; background: var(--voc-surface); color: var(--voc-blue); }
      .dialog-icon ha-icon { --mdc-icon-size: 23px; }
      .confirm-dialog h3 { margin: 0; font-size: 17px; font-weight: 500; }
      .confirm-dialog p { margin: 8px 0 20px; color: var(--voc-secondary); font-size: 13px; line-height: 1.5; }
      .dialog-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 9px; }
      .dialog-actions button { min-height: 42px; border: 0; border-radius: 10px; cursor: pointer; }
      .dialog-cancel { background: var(--voc-surface); color: var(--voc-text); }
      .dialog-confirm { background: var(--voc-blue); color: #fff; }
      .feedback {
        position: absolute;
        z-index: 8;
        left: 50%;
        bottom: 14px;
        min-height: 38px;
        max-width: calc(100% - 32px);
        border: 1px solid color-mix(in srgb, var(--voc-positive) 28%, transparent);
        border-radius: 19px;
        padding: 0 13px;
        display: flex;
        align-items: center;
        gap: 7px;
        transform: translateX(-50%);
        background: color-mix(in srgb, var(--voc-bg) 90%, var(--voc-positive));
        box-shadow: 0 10px 26px rgba(0, 0, 0, .18);
        color: var(--voc-success);
        font-size: 11px;
        white-space: nowrap;
        animation: voc-toast-in .24s cubic-bezier(.2, .8, .2, 1) both;
      }
      .feedback[hidden] { display: none; }
      .feedback ha-icon { --mdc-icon-size: 16px; flex: 0 0 auto; }
      .feedback span { overflow: hidden; text-overflow: ellipsis; }
      .feedback.error { border-color: color-mix(in srgb, var(--voc-warning) 30%, transparent); background: color-mix(in srgb, var(--voc-bg) 90%, var(--voc-warning)); color: var(--voc-danger); }
      .setup-card { min-height: 130px; padding: 24px; display: flex; align-items: center; gap: 14px; }
      .setup-card > ha-icon { --mdc-icon-size: 34px; color: var(--voc-blue); }
      .setup-card div { display: flex; flex-direction: column; gap: 4px; }
      .setup-card strong { font-weight: 500; }
      .setup-card span { color: var(--voc-secondary); font-size: 12px; }
      button:focus-visible { outline: 2px solid var(--voc-blue); outline-offset: 2px; }
      @media (hover: hover) {
        .range-tile:hover { transform: translateY(-1px); border-color: color-mix(in srgb, var(--voc-blue) 24%, var(--voc-line)); box-shadow: 0 10px 22px rgba(0, 0, 0, .07); }
        .lock-pill:not(:disabled):hover { transform: translateY(-1px); box-shadow: 0 8px 16px rgba(0, 0, 0, .12); }
        .state-row:not(.missing):hover, .trip-row button:hover { background: color-mix(in srgb, var(--voc-blue) 5%, transparent); }
        .control:not(:disabled):hover { transform: translateY(-2px); border-color: color-mix(in srgb, var(--voc-blue) 20%, var(--voc-line)); box-shadow: 0 10px 20px rgba(0, 0, 0, .08); }
        .control:not(:disabled):hover .control-icon { transform: scale(1.04); }
      }
      .range-tile:active, .lock-pill:not(:disabled):active, .control:not(:disabled):active { transform: scale(.98); }
      .state-heading small.warn::before, .connection.offline i { animation: voc-status-pulse 1.7s ease-in-out infinite; }
      @keyframes voc-progress-in { from { transform: scaleX(0); } to { transform: scaleX(1); } }
      @keyframes voc-car-in { from { opacity: 0; transform: translateY(8px) scale(.985); } to { opacity: 1; transform: translateY(0) scale(1); } }
      @keyframes voc-warning-in { from { opacity: 0; } to { opacity: 1; } }
      @keyframes voc-spin { to { transform: rotate(360deg); } }
      @keyframes voc-status-pulse { 0%, 100% { opacity: 1; box-shadow: 0 0 0 0 currentColor; } 50% { opacity: .62; box-shadow: 0 0 0 4px transparent; } }
      @keyframes voc-dialog-in { from { opacity: 0; transform: translateY(8px) scale(.98); } to { opacity: 1; transform: translateY(0) scale(1); } }
      @keyframes voc-toast-in { from { opacity: 0; transform: translate(-50%, 8px) scale(.98); } to { opacity: 1; transform: translate(-50%, 0) scale(1); } }
      @container (max-width: 620px) {
        .hero { padding: 19px 16px 14px; }
        h2 { font-size: 24px; }
        .lock-pill { min-width: 88px; min-height: 44px; padding-inline: 13px; }
        .range-band { margin: 0 16px; }
        .range-tile { min-height: 96px; padding: 12px 13px 13px; }
        .range-tile > strong { font-size: 22px; }
        .vehicle-area { grid-template-columns: minmax(0, 1.05fr) minmax(132px, .95fr); gap: 12px; margin: 14px 16px 17px; padding: 14px 13px 15px; }
        .car-canvas { width: min(100%, 188px); }
        .statistics, .controls-wrap { padding-inline: 16px; }
        .controls { grid-template-columns: repeat(4, minmax(0, 1fr)); }
      }
      @container (max-width: 350px) {
        .lock-pill { min-width: 44px; width: 44px; padding: 0; }
        .lock-pill span { display: none; }
        .hero-meta { gap: 7px; }
        .vehicle-area { grid-template-columns: 1fr; }
        .car-canvas { width: 188px; }
        .state-panel { width: 100%; }
        .trip-labels, .trip-row { grid-template-columns: 32px repeat(4, minmax(0, 1fr)); }
        .trip-row button strong { font-size: 10px; }
      }
      @media (prefers-reduced-motion: reduce) {
        *, *::before, *::after { scroll-behavior: auto !important; animation-duration: .001ms !important; animation-iteration-count: 1 !important; transition-duration: .001ms !important; }
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
    name: "Volvo 原生车辆控制卡",
    description: "内置黑色 S90/XC60/XC90 车模的车辆状态、双能源续航、TM/TA 与远程控制卡。",
    preview: true,
    documentationURL: "https://github.com/Annincikee/hass-volvooncall-cn",
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
