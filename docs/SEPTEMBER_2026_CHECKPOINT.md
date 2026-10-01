# September 2026 compatibility work and manufacturer review
Updated: 1 October 2026. Documentation checkpoint; not a release or security certification.

## Scope and evidence
This record consolidates the 30 September replacement-dimmer investigation, the existing Alexa installation, the repository ZIP review and the manufacturer-review request. Live controller changes were run by the owner and assessed through logs and screenshots. The actual patched production sources have not yet been retrieved and compared with GitHub.

The repository remains private. Publication is on hold pending Varilight review. This documentation update does not change running services or claim that the compatibility patches are present in this checkout.

## Project and owner introduction
MKSI (MK Smart Integration) is Joakim's personal smart-home integration project, focused on local control, Home Assistant and making existing devices work together with fewer separate apps and cloud dependencies. Joakim works in software engineering on HPE Slingshot networking/HPC infrastructure. This is a personal project, not an HPE product or an endorsed Varilight integration.

## Compatibility findings
| Item | Earlier installed dimmers | Replacement observed 30 September |
| --- | --- | --- |
| Reported firmware | 2.8.55 | 26.08 |
| Observed transport | TLS 1.1 / AES128-SHA | TLS 1.2 / ECDHE-RSA-AES128-GCM-SHA256 |
| SUPLA packet version | 12 | 25 |
| Registration call | E / 69 | G / 76 |
| Channel record | C / 25 bytes | E / 36 bytes |
| Registration reply | 70 / 7-byte payload | B / 71 / 10-byte payload for channel zero |

These are observations of the tested units, not a specification for every product or firmware revision. Native MQTT settings were visible on the replacement but were not used or validated. The implemented path remains dimmer -> local SUPLA-compatible controller -> MQTT -> Home Assistant.

### Address and TLS
An initial mistyped server address was corrected. TCP connectivity then reached the listener on port 2016, which logged NO_SHARED_CIPHER. A packet capture contained 30 packets with no kernel drops. The replacement ClientHello offered TLS 1.2 and cipher 0xc02f, but not the static AES128-SHA allowed by the old listener.

The production patch supplied to the owner changed the TLS context to:

```python
ctx.minimum_version = ssl.TLSVersion.TLSv1_1
ctx.maximum_version = ssl.TLSVersion.TLSv1_2
ctx.set_ciphers("ECDHE-RSA-AES128-GCM-SHA256:AES128-SHA:@SECLEVEL=0")
```

Post-change logs showed TLSv1.2 with ECDHE-RSA-AES128-GCM-SHA256. Legacy support was retained for the earlier units. The dimmer had skip certificate verification selected; this does not remove the need for matching TLS versions and ciphers. The retained TLS 1.1, lowered security level and certificate-verification bypass remain security limitations.

### Registration
After TLS succeeded, logs showed packet version 25, call 76, payload length 620, ignored before enrollment. The previous controller accepted only registration E and protocol 12.

The supplied production patch:
- Added CALL_REGISTER_G=76 and CALL_REGISTER_RESULT_B=71.
- Added G decoding with the shared 584-byte fixed header and 36-byte channel-E record.
- Used channel format `<BiiiqBI8sBB`, including 64-bit flags, offline/validity fields, value, icon and subdevice ID.
- Added reply-B header `<iBBBH` plus channel report byte 0x01 for the supported single channel zero.
- Accepted only known pairs (E,12) and (G,25), selecting the matching decoder and reply.
- Preserved single-channel, channel-zero, type-4000, brightness-range and enrollment checks and registration-payload redaction.

The existing global protocol constant 28 was not changed. This does not imply full implementation of all optional protocol-25 or protocol-28 calls.

The patch included exact-match edit guards, source compilation, synthetic E/G decoding, high 64-bit flag preservation, rejection of short/overlong payloads and reply call/length checks. Timestamped backups were made beside the sources. Exact backup names and final source hashes were not captured in the conversation.

### Observed result and limits
The replacement registered as LivingRoom. Controller UI showed it online at 63% with origin commanded, and HA subsequently showed 62%. Cooking and Dining were online after restart. MQTT diagnostics showed brightness command transmissions.

These observations establish registration, discovery and reported state, not complete physical acceptance or a fix for the prior spontaneous-off issue. Required remaining checks include physical on/off and dimming, wall-control feedback, reconnect behaviour and sustained operation.

Later owner reports described Dining switching itself off, remaining on for hours at 12%, and a possible brightness-dependent pattern that could also be triggered using the wall dimmer. The approximate 30% threshold was tentative. Loads were reported as Cooking two LEDs, Dining four and LivingRoom eight. These are troubleshooting observations, not proof of overheating, firmware fault or controller causation. Proposed swaps/rewiring were not recorded as completed. No electrical change is prescribed by this document.

## Home Assistant and Alexa
The replacement entity is `light.livingroom`; do not use the previous LivingRoom entity suffix from historical notes. Runtime topics follow `mksi/lights/livingroom/` for availability, state, brightness commands and diagnostics.

### Accepted baseline on 28 September
- Home Assistant 2026.8.3; Matterbridge HA app 2026.9.4; runtime 3.10.11; matterbridge-hass 1.5.0.
- One bridge, Alexa fabric already commissioned.
- Cooking: `light.varilight_v_pro_smart_dimmer_2`, endpoint 4.
- Dining: `light.varilight_v_pro_smart_dimmer_3`, endpoint 5.
- Owner accepted Alexa on/off, brightness, correct Cooking control and HA-originated state updates.
- Split name strategy Entity name; controller strategy Merge.
- Maintenance/virtual Restart and Update endpoints disabled.
- App automatic updates off; force_update=false. This is not an immutable dependency lock.
- Software default VID/PID 0xFFF1/0x8000, not a commercial Matter certification claim.

### LivingRoom addition on 30 September
Read back on 1 October through MKSI Agent from the operator's INSTALL_PROGRESS.md:
- Added `light.livingroom` to both whiteList and splitEntities.
- Removed the prior LivingRoom blacklist entry.
- Restarted only Matterbridge; preserved Alexa fabric.
- Verified exactly three registered devices, with LivingRoom endpoint 6 reachable and exposing BridgedNode/DimmableLight, OnOff and LevelControl.
- Runtime/plugin remained 3.10.11/1.5.0.
- Alexa-side appearance, voice control and physical/state-update acceptance for LivingRoom still await owner verification.

Current recorded selections:
```json
{
  "whiteList": ["light.varilight_v_pro_smart_dimmer_2", "light.varilight_v_pro_smart_dimmer_3", "light.livingroom"],
  "splitEntities": ["light.varilight_v_pro_smart_dimmer_2", "light.varilight_v_pro_smart_dimmer_3", "light.livingroom"]
}
```

Preserve persistent Matterbridge state at `/addons/matterbridge/.matterbridge`. Addition rollback removes only LivingRoom from both arrays, restores its prior blacklist entry and restarts Matterbridge. Do not reset the existing fabric or physical dimmers. The unrelated Govee discovery card was left outside scope.

## Repository and security review
The supplied repository ZIP predates the TLS/G changes. Its 55 existing offline tests passed on 30 September; that result does not cover the replacement patches. A heuristic inspection found no obvious embedded credentials, private keys or firmware binaries, but was not an exhaustive security or provenance audit.

The baseline parses the registration AuthKey but does not validate it. Registration trusts enrolled GUID identity. A network client able to reach the listener and supply an enrolled GUID with an accepted registration layout could impersonate that device, replace its session, submit false state or receive commands. Enrollment is not cryptographic device authentication.

Other documented limits include an unauthenticated management UI intended for a trusted network, legacy TLS compatibility and the need for restricted MQTT credentials/ACLs. Keep the services off the public Internet. No claim of security approval is made.

On 1 October, MKSI Agent found two modified files in the local Git staging checkout: controller.py and test_controller.py, both concerning the previously added MQTT on_command_type=brightness fix. GitHub already contains that fix. Those local edits were preserved; this documentation task does not reset or stage unrelated code.

## Varilight review and development provenance
George Fry's message dated 30 September requested a repository ZIP and asked:
- Whether proprietary Varilight firmware/code had entered through AI tools.
- Whether the implementation could compromise devices or customer installations.
- How much the owner personally reviewed and understood line by line versus AI-generated implementation accepted after hardware tests.
- That publication wait for review, including product, compatibility and SUPLA wording.

The request is not approval. No email or review package has been sent by this documentation task.

AI tools were involved in implementation and analysis, with the owner directing investigation and testing hardware. The exact proportion of personal line-by-line review is not established and must be supplied honestly by the owner. Do not invent a percentage or claim a full manual/security audit. Public SUPLA protocol definitions were consulted; complete provenance/licensing remains to be checked. Absence of firmware binaries does not prove the provenance of every source line.

A review email may explain that the earlier ZIP describes the earlier firmware baseline, while TLS 1.2/cipher support and protocol-25 registration-G support were subsequently added to the live controller. Label those changes as observations and implementation changes, not official Varilight release notes.

Prepare any manufacturer ZIP from an identified private commit, excluding credentials, private keys, raw registration payloads, personal logs and operational backups. Include known security limits and distinguish repository contents from live-only changes.

## Workflow and next steps
The owner requested the existing ChatGPT -> MKSI Agent -> Peasant workflow. MKSI_AGENT.md was read first. Agent health confirmed gateway, worker and Peasant healthy on 1 October. Deterministic Agent list/read/git actions retrieved current operator records. Peasant is used only for bounded read-only documentation review; its output is independently checked. Agent has no write/commit/push endpoint, so reviewed edits and GitHub writes use existing deterministic tools.

1. Retrieve actual production controller.py/protocol.py and identify backups without exposing private config.
2. Compare with the private repository; port reviewed TLS/G changes and add persistent old/new regression tests.
3. Confirm LivingRoom Alexa voice control and physical/state feedback; retain the accepted Cooking/Dining baseline.
4. Continue spontaneous-off diagnosis without conflating connectivity with electrical/hardware stability.
5. Complete provenance/security review and the owner's account of human review.
6. Prepare a commit-pinned source ZIP for Varilight. Keep publication on hold.

Controller rollback requires a matched, explicitly selected backup pair. Reverting registration-G or TLS support will remove replacement compatibility. Preserve current sources before restoration and verify old units afterwards. The earlier HA backup is not a backup of CT109 controller sources.

## Sources
- Owner-pasted commands, packet dump, screenshots and correspondence, 30 September.
- MKSI_VARILIGHT_CHECKPOINT_2026-09-30.md, historical session record.
- Operator operations/alexa-local-bridge/INSTALL_PROGRESS.md, read via Agent on 1 October.
- Operator operations/varilight-local-integration/LIVE_DEPLOYMENT.md, older 25 September baseline, not current inventory.
- Repository ZIP and GitHub main inspected for scope.
- [SUPLA protocol definitions](https://github.com/SUPLA/supla-core/blob/master/supla-common/proto.h), consulted during patch work; exact upstream revision was not pinned.
