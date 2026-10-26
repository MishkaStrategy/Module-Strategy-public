# Local rollback plan

## Scope

This procedure rolls back the Router module locally. It does not modify GitHub,
remote branches, Pull Requests, tags, Actions, or exchange infrastructure.

## Before controlled integration

1. Record the accepted local commit and wheel SHA-256.
2. Keep the previous known-good Router and core wheels.
3. Preserve the configuration schema/hash and migration notes.
4. Verify that no downstream component treats a composed plan as executed state.

## Rollback procedure

1. Stop feeding new decision packages to the candidate module.
2. Allow the Execution Engine to handle or explicitly reject already delivered
   plans according to their immutable preconditions and expiry.
3. Uninstall the candidate Router wheel from the isolated environment.
4. Install the previous known-good wheel with `PIP_NO_INDEX=1 --no-deps` together
   with its compatible core dependency set.
5. Run `pip check`, entry-point discovery, manifest readback, deterministic smoke,
   and one accepted plus one rejected golden fixture.
6. Restore the prior versioned configuration. Do not reuse a configuration hash
   from the candidate version.
7. Resume only after portfolio/order/risk snapshots have been refreshed.

## Data considerations

The Router is stateless, so it has no checkpoint database to migrate back.
Downstream execution state, reservations, and exchange orders are outside this
module and must not be deleted or rewritten by the rollback procedure.
