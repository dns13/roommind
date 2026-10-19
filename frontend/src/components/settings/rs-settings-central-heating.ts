/**
 * rs-settings-central-heating – Central heating: a boiler switched by the number of rooms demanding heat.
 */
import { html, nothing } from "lit";
import { RsSettingsBase } from "./rs-settings-base";
import { customElement, property } from "lit/decorators.js";
import type { CentralHeatingSettings, HomeAssistant } from "../../types";
import { localize, type TranslationKey } from "../../utils/localize";
import { getSelectValue } from "../../utils/events";

export const DEFAULT_CENTRAL_HEATING: CentralHeatingSettings = {
  enabled: false,
  entity_id: "",
  min_rooms: 1,
  start_delay_s: 0,
  stop_delay_s: 0,
  on_temperature: null,
  off_temperature: null,
  climate_off_mode: "temperature",
};

@customElement("rs-settings-central-heating")
export class RsSettingsCentralHeating extends RsSettingsBase {
  @property({ attribute: false }) public hass!: HomeAssistant;
  @property({ attribute: false }) public centralHeating: CentralHeatingSettings =
    DEFAULT_CENTRAL_HEATING;

  render() {
    const l = this.hass.language;
    const c = this.centralHeating;
    const isClimate = c.entity_id.startsWith("climate.");

    return html`
      <div class="toggle-row">
        <div class="toggle-text">
          <span class="toggle-label">${localize("central_heating.enabled", l)}</span>
          <span class="toggle-hint">${localize("central_heating.enabled_hint", l)}</span>
        </div>
        <ha-switch
          .checked=${c.enabled}
          @change=${(e: Event) => this._update({ enabled: (e.target as HTMLInputElement).checked })}
        ></ha-switch>
      </div>

      ${
        c.enabled
          ? html`
              <div class="settings-section">
                <ha-entity-picker
                  .hass=${this.hass}
                  .value=${c.entity_id}
                  .includeDomains=${["switch", "input_boolean", "climate"]}
                  .entityFilter=${this._entityFilter}
                  .label=${localize("central_heating.entity", l)}
                  @value-changed=${(e: CustomEvent) => this._update({ entity_id: e.detail?.value ?? "" })}
                ></ha-entity-picker>
                <span class="field-hint">${localize("central_heating.entity_hint", l)}</span>
              </div>

              <div class="threshold-grid">
                ${this._numberField("min_rooms", c.min_rooms, 1, 50, "central_heating.min_rooms", "")}
                ${this._numberField(
                  "start_delay_s",
                  c.start_delay_s,
                  0,
                  3600,
                  "central_heating.start_delay",
                  "s",
                )}
                ${this._numberField("stop_delay_s", c.stop_delay_s, 0, 3600, "central_heating.stop_delay", "s")}
              </div>
              <span class="field-hint">${localize("central_heating.delays_hint", l)}</span>

              ${isClimate ? this._renderThermostat(l) : nothing}
            `
          : nothing
      }
    `;
  }

  private _renderThermostat(l: string) {
    const c = this.centralHeating;
    return html`
      <div class="settings-section">
        <div class="threshold-grid">
          ${this._tempField("on_temperature", c.on_temperature, "central_heating.on_temperature")}
          ${this._tempField("off_temperature", c.off_temperature, "central_heating.off_temperature")}
        </div>
        <span class="field-hint">${localize("central_heating.temperatures_hint", l)}</span>
        <ha-select
          style="margin-top: 12px"
          .label=${localize("central_heating.off_mode", l)}
          .value=${c.climate_off_mode}
          .options=${[
            { value: "temperature", label: localize("central_heating.off_mode_temperature", l) },
            { value: "off", label: localize("central_heating.off_mode_off", l) },
          ]}
          fixedMenuPosition
          @selected=${(e: Event) => {
            const value = getSelectValue(e) as CentralHeatingSettings["climate_off_mode"];
            if (value && value !== this.centralHeating.climate_off_mode)
              this._update({ climate_off_mode: value });
          }}
          @closed=${(e: Event) => e.stopPropagation()}
        >
          <ha-list-item value="temperature"
            >${localize("central_heating.off_mode_temperature", l)}</ha-list-item
          >
          <ha-list-item value="off">${localize("central_heating.off_mode_off", l)}</ha-list-item>
        </ha-select>
      </div>
    `;
  }

  private _numberField(
    key: "min_rooms" | "start_delay_s" | "stop_delay_s",
    value: number,
    min: number,
    max: number,
    labelKey: TranslationKey,
    suffix: string,
  ) {
    return html`
      <div class="threshold-field">
        <ha-textfield
          .value=${String(value)}
          .label=${localize(labelKey, this.hass.language)}
          .suffix=${suffix}
          type="number"
          step="1"
          min=${min}
          max=${max}
          @change=${(e: Event) => {
            const v = parseInt((e.target as HTMLInputElement).value, 10);
            if (!isNaN(v) && v >= min && v <= max) this._update({ [key]: v });
          }}
        ></ha-textfield>
      </div>
    `;
  }

  private _tempField(
    key: "on_temperature" | "off_temperature",
    value: number | null,
    labelKey: TranslationKey,
  ) {
    return html`
      <div class="threshold-field">
        <ha-textfield
          .value=${value == null ? "" : String(value)}
          .label=${localize(labelKey, this.hass.language)}
          suffix="°C"
          type="number"
          step="0.5"
          min="5"
          max="90"
          @change=${(e: Event) => {
            const raw = (e.target as HTMLInputElement).value;
            const v = parseFloat(raw);
            if (raw === "") this._update({ [key]: null });
            else if (!isNaN(v) && v >= 5 && v <= 90) this._update({ [key]: v });
          }}
        ></ha-textfield>
      </div>
    `;
  }

  private _entityFilter = (entity: { entity_id: string }): boolean =>
    !entity.entity_id.substring(entity.entity_id.indexOf(".") + 1).startsWith("roommind_");

  private _update(changes: Partial<CentralHeatingSettings>) {
    this._fire("centralHeating", { ...this.centralHeating, ...changes });
  }

  static styles = [RsSettingsBase.settingsBaseStyles];
}

declare global {
  interface HTMLElementTagNameMap {
    "rs-settings-central-heating": RsSettingsCentralHeating;
  }
}
