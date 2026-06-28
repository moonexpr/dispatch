# usecases — WebsiteWF use-case overlays package.
#
# Each module here registers ONE use case's web:* action bodies on top of
# baseworkflow's fully-populated TokenRegistry (see src/websitewf/bindings.py for
# the proof-vertical analogue). Modules are disjoint so multiple overlays never
# collide. The WebsiteWF engine selects one by WEBSITEWF_USECASE (Integrate phase).
