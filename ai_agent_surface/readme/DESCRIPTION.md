An AI agent that calls Odoo business methods on a user's behalf is constrained
by exactly one thing: what that user's access rights let it reach. This module
measures that surface and ships the assertions that prove the measurement is
honest.

Given a user, it enumerates every model that user can read and every method on
those models that `/json/2/<model>/<method>` would actually dispatch to, then
classifies each as read-only, writing, or irreversible. The result answers a
question the community has been designing around without a number:

> an agent with scope `rpc`, acting as this user, can call *N* methods across
> *M* models, of which *K* are irreversible.

The figure is per-database — it depends on which modules are installed — which
is why this ships as a scan you can run rather than a number quoted in a
document.

The module adds no menu, no view and no stored data. Its scan method is
deliberately private: a module whose purpose is auditing the remotely-callable
surface must not add itself to it, and there is a test asserting `/json/2`
refuses it.
