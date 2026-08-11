# Homelab architecture (example)

Replace this file with your real map as `skills/homelab-architecture.md` (gitignored)
or keep editing this example for demos.

## Typical roles
- **Homelab host** (`192.168.x.x`): Home Assistant, Zigbee2MQTT, MQTT, Portainer, MeshCentral, Butler (`:8788`)
- **Windows nodes** (`:8789` Butler Node probe): file manager / terminal / optional MeshAgent desktop

## Guidance for Aura
- Discover live state with tools (`search_ha_devices`, `list_homelab_nodes`, health APIs).
- Do not invent device names or IPs that are not in config / tool results.
- Prefer short actionable replies in the configured reply language.
