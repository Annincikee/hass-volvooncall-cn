import assert from "node:assert/strict";
import test from "node:test";

const registry = new Map();

class FakeHTMLElement {
  constructor() {
    this.isConnected = true;
    this.shadowRoot = null;
  }

  attachShadow() {
    this.shadowRoot = {
      innerHTML: "",
      querySelector: () => null,
      querySelectorAll: () => [],
    };
    return this.shadowRoot;
  }

  dispatchEvent() {}
}

globalThis.HTMLElement = FakeHTMLElement;
globalThis.customElements = {
  define: (name, constructor) => registry.set(name, constructor),
  get: (name) => registry.get(name),
};
globalThis.window = {
  confirm: () => true,
  customCards: [],
};

await import(
  "../../custom_components/volvooncall_cn/frontend/volvo-car-card.js"
);

const VolvoCarCard = registry.get("volvo-car-card");

test("registers the custom card and modern HA metadata", () => {
  assert.ok(VolvoCarCard);
  assert.equal(window.customCards.length, 1);
  assert.equal(window.customCards[0].type, "volvo-car-card");
  assert.equal(typeof window.customCards[0].getEntitySuggestion, "function");
  assert.equal(typeof VolvoCarCard.getConfigForm, "function");
});

test("uses S90 T8 defaults and derives entities from VIN", () => {
  const card = new VolvoCarCard();
  card.setConfig({ vin: "TESTVIN0000000001" });

  assert.equal(card._config.model, "s90_t8");
  assert.equal(card._config.name, "S90 T8");
  assert.equal(
    card._entityId("battery"),
    "sensor.testvin0000000001_battery_charge_level",
  );
  assert.equal(
    card._entityId("front_left_door"),
    "binary_sensor.testvin0000000001_front_left_door",
  );
  assert.deepEqual(card.getGridOptions(), {
    rows: 9,
    columns: 12,
    min_rows: 6,
    min_columns: 6,
  });
});

test("supports per-entity overrides for renamed HA entities", () => {
  const card = new VolvoCarCard();
  card.setConfig({
    vin: "TESTVIN0000000001",
    entities: {
      battery: "sensor.s90_t8_battery",
    },
  });

  assert.equal(card._entityId("battery"), "sensor.s90_t8_battery");
  assert.equal(
    card._entityId("electric_range"),
    "sensor.testvin0000000001_electric_range",
  );
  assert.equal(
    card._entityId("full_charge_range"),
    "sensor.testvin0000000001_full_charge_electric_range",
  );
});

test("suggests an S90 T8 card from a Volvo entity", () => {
  const suggestion = window.customCards[0].getEntitySuggestion(
    {},
    "lock.testvin0000000001_lock",
  );

  assert.equal(suggestion.config.type, "custom:volvo-car-card");
  assert.equal(suggestion.config.vin, "TESTVIN0000000001");
  assert.equal(suggestion.config.model, "s90_t8");
});

test("calls unlock only after confirmation", async () => {
  const calls = [];
  let confirmed = false;
  window.confirm = () => {
    confirmed = true;
    return true;
  };

  const card = new VolvoCarCard();
  card.setConfig({ vin: "TESTVIN0000000001" });
  card.hass = {
    states: {
      "lock.testvin0000000001_lock": {
        entity_id: "lock.testvin0000000001_lock",
        state: "locked",
        attributes: {},
      },
    },
    callService: async (...args) => calls.push(args),
  };

  await card._runAction("lock");

  assert.equal(confirmed, true);
  assert.deepEqual(calls, [
    ["lock", "unlock", { entity_id: "lock.testvin0000000001_lock" }],
  ]);
});

test("maps open body parts to the vehicle overlay", () => {
  const card = new VolvoCarCard();
  card.setConfig({ vin: "TESTVIN0000000001" });
  card.hass = {
    states: {
      "binary_sensor.testvin0000000001_front_left_door": {
        entity_id: "binary_sensor.testvin0000000001_front_left_door",
        state: "on",
        attributes: {},
      },
      "binary_sensor.testvin0000000001_tail_gate": {
        entity_id: "binary_sensor.testvin0000000001_tail_gate",
        state: "on",
        attributes: {},
      },
    },
  };

  assert.deepEqual(
    card._openParts().map(([key]) => key),
    ["front_left_door", "tailgate"],
  );
});

test("cancelling a dangerous action does not call a service", async () => {
  const calls = [];
  window.confirm = () => false;
  const card = new VolvoCarCard();
  card.setConfig({ vin: "TESTVIN0000000001" });
  card.hass = {
    states: {
      "switch.testvin0000000001_engine_remote_control": {
        entity_id: "switch.testvin0000000001_engine_remote_control",
        state: "off",
        attributes: {},
      },
    },
    callService: async (...args) => calls.push(args),
  };

  await card._runAction("engine_control");

  assert.deepEqual(calls, []);
});
