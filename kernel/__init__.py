"""kernel — concrete runtime engines (implementations of foundation.kernel interfaces).

foundation.kernel declares the contracts (Session / BootSpec / SessionKind + the daemon
interface); this repo-root package holds the concrete engines that implement them. Only
the adhoc engine exists today (``kernel.adhoc_session.AdhocSession``); a concrete daemon
is future work (TODO(John), see foundation.kernel.daemon).
"""
