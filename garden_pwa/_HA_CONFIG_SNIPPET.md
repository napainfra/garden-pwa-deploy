# HA Config Snippet for Garden PWA v1.3.0
## Timelapse render shell_command + nightly automation

Paste the following into `/config/configuration.yaml` and reload the HA configuration (Developer Tools → YAML → All YAML Configuration, or run `ha core restart`).

---

### `configuration.yaml` additions

```yaml
shell_command:
  render_garden_timelapse: >
    bash -c "DATE=$(date -d yesterday +%Y-%m-%d);
    mkdir -p /media/garden_timelapse &&
    cd /media/garden_timelapse/$DATE &&
    ffmpeg -y -framerate 24 -pattern_type glob -i '*.jpg'
    -c:v libx264 -pix_fmt yuv420p -movflags +faststart
    /media/garden_timelapse/$DATE.mp4
    2>/media/garden_timelapse/${DATE}_render.log"
```

> **Note:** The above is split for readability. In YAML, the `>` block scalar collapses newlines — it will be treated as one long command. If your HA version requires a literal single-line string, paste as:
> ```yaml
> shell_command:
>   render_garden_timelapse: 'bash -c "DATE=$(date -d yesterday +%Y-%m-%d); mkdir -p /media/garden_timelapse && cd /media/garden_timelapse/$DATE && ffmpeg -y -framerate 24 -pattern_type glob -i ''*.jpg'' -c:v libx264 -pix_fmt yuv420p -movflags +faststart /media/garden_timelapse/$DATE.mp4 2>/media/garden_timelapse/${DATE}_render.log"'
> ```

---

### Nightly automation (automations.yaml or via UI)

```yaml
automation:
  - alias: "Garden timelapse render (nightly 23:00)"
    id: garden_timelapse_render_nightly
    trigger:
      - platform: time
        at: "23:00:00"
    condition: []
    action:
      - service: shell_command.render_garden_timelapse
    mode: single
```

You can also create this via the HA UI:
1. Settings → Automations → Create automation → Edit as YAML
2. Paste the `trigger` + `action` blocks above.

---

### HP10 snapshot automation (already running — for reference)

The HP10 WittCam snapshot automation saves to `/media/garden_timelapse/YYYY-MM-DD/HHMM.jpg` every 5 minutes. This is the input for the nightly timelapse render.

- Camera entity: `camera.192_168_50_52`
- Snapshot path pattern: `/media/garden_timelapse/{date}/{time}.jpg`

---

### Entity name corrections discovered from `_live_entities.json`

| Constant | Entity ID used | Notes |
|---|---|---|
| `E_OUTDOOR_T` | `sensor.hp2564bu_pro_v2_1_1_outdoor_temperature` | ✓ confirmed |
| `E_OUTDOOR_H` | `sensor.hp2564bu_pro_v2_1_1_humidity` | outdoor humidity (not indoor) |
| `E_WIND_SPEED` | `sensor.hp2564bu_pro_v2_1_1_wind_speed` | in **m/s** — code converts × 3.6 → km/h |
| `E_WIND_GUST` | `sensor.hp2564bu_pro_v2_1_1_wind_gust` | in **m/s** — code converts × 3.6 → km/h |
| `E_WIND_DIR` | `sensor.hp2564bu_pro_v2_1_1_wind_direction` | degrees 0-360, code converts to compass |
| `E_UV` | `sensor.hp2564bu_pro_v2_1_1_uv_index` | integer in live data |
| `E_LUX` | `sensor.hp2564bu_pro_v2_1_1_solar_lux` | ✓ confirmed |
| `E_RADIATION` | `sensor.hp2564bu_pro_v2_1_1_solar_radiation` | ✓ confirmed |
| `E_SOIL_T_TOMATO` | `sensor.hp2564bu_pro_v2_1_1_soil_temperature_1` | ✓ CH1 WN34S |
| `E_SOIL_M_TOMATO` | `sensor.hp2564bu_pro_v2_1_1_soil_moisture_1` | ✓ CH1 |
| `E_SOIL_B_TOMATO` | `sensor.hp2564bu_pro_v2_1_1_soil_battery_1` | ✓ CH1 |
| `E_SOIL_M_CUCUMBER` | `sensor.hp2564bu_pro_v2_1_1_soil_moisture_2` | ✓ CH2 WH51 |
| `E_SOIL_B_CUCUMBER` | `sensor.hp2564bu_pro_v2_1_1_soil_battery_2` | ✓ CH2 |
| (no CH2 temp) | — | CH2 is plain WH51 — no temperature probe |

**Piezo rain ignored:** `*_rain_piezo` entities are present but miscalibrated (`monthly_rain_piezo` shows 1000mm). Code uses WH40 tipping bucket only.

---

### Manual shell command to run timelapse on-demand (for testing)

```bash
# Run from HA terminal / SSH:
ha shell_command render_garden_timelapse
# Or via Developer Tools → Services → shell_command.render_garden_timelapse
```

### Post-install checklist

1. Paste shell_command + automation YAML into `/config/configuration.yaml`
2. Restart HA core or reload configuration
3. Verify `shell_command.render_garden_timelapse` appears in Developer Tools → Services
4. Test by calling the service manually
5. Check `/media/garden_timelapse/` for output mp4 files the next morning
