# Web Console Release Contract v1

**Disposition: pending draft.** The contract shape is source-only preparation; it is not a release decision, deployment instruction, CI request, or evidence of Linux/Darwin acceptance. The sample envelope is [WEB_CONSOLE_RELEASE_CONTRACT_V1.json](WEB_CONSOLE_RELEASE_CONTRACT_V1.json).

## Purpose and boundary

The existing package descriptor declares `node >=22.12.0`, the npm lock is format 3, and `npm run build` invokes `tsc --noEmit && vite build`. That produces static `dist` assets, while the repository does not select a production serving stack. These facts do not choose an exact Node/npm pair, trusted provisioning material, build image/architecture, registry policy, lifecycle-script policy, Linux serving runtime, or Darwin browser context.

The v1 sidecar gives those decisions typed, hash-bound fields. It links to one exact v2 manifest by its canonical digest, candidate/profile identities, and source commit. Its Linux build record binds the exact Node/npm toolchain, immutable provisioning material, registry/source policy, npm lock relation, script policy, source/config input hashes, isolated reconstruction receipt, and deterministic ZIP artifact digest plus sorted member manifest. Linux serving and deployment are separately bound to the artifact and a concrete runtime/configuration context. Darwin is an acceptance-only context with explicit browser contexts. It either consumes the identical Linux artifact digest or records a distinct source-bound Darwin rebuild and its own evidence; Darwin evidence cannot substitute for Linux deployment evidence.

## Fail-closed status

The JSON Schema and structural validator allow a pending draft that enumerates every unresolved binding. An accepted sidecar must bind every field, match the linked v2 identities, validate the npm lock against `package.json`, match every tracked console input to the selected source commit, verify the isolated reconstruction receipt, verify packaged ZIP member bytes against the deterministic member manifest, and keep Linux deployment and Darwin browser evidence in their declared contexts.

This sidecar is not yet integrated into a version-3 canonical release envelope or the protected combined release verifier. The current v2 envelope remains readable and retains its existing acceptance blockers; a sidecar does not resolve a v2 `unknown-blocking` Console scope, replace any v2 obligation, or independently confer manifest/release acceptance. The local structural helper calls the existing v2 validator after checking the sidecar, but the release CLI and protected verifier do not yet load this file as one canonical candidate graph. The next release-contract change must version and integrate the console record into that combined path, while preserving one Linux/VDS deployment target, Darwin acceptance-only mappings, and the separate CPython 3.12/3.13 scopes. Until that integration and trusted verification exist, no v1 sidecar disposition can be treated as acceptance.

## Still pending

The owner must select the exact Node and npm versions; immutable provisioning/material identity; Linux build architecture and digest-pinned image; registry/source and install-script policies; frozen source-input hashes; isolated reconstruction; packaged artifact; serving mode, runtime/image and configuration; Linux deployment context; Darwin host/browser contexts; and artifact-consumption mode. None is inferred by the sample. No SEARCH, HOLDOUT, PAPER, soak, deployment, publication, CI, or release action is authorized by this draft.
