from dataclasses import dataclass


PROCESSING_MODES = {
    "optimized",
    "window_background",
    "opening_background",
    "original",
    "configurable",
}
PROCESSING_REQUIRED_MODES = {"optimized", "window_background", "opening_background"}
MASKED_BACKGROUND_MODES = {"window_background", "opening_background"}
ORIENTATION_CATEGORIES = {"exterior", "interior", "detail", "special"}
WINDOW_MASK_PROMPT = "windshield, side window"


def mask_prompt_defaults(orientation_key: str, processing_mode: str) -> tuple[str, str]:
    """Return the visible system defaults for semantic mask selection and protection."""
    if orientation_key == "steering-wheel":
        return (
            WINDOW_MASK_PROMPT,
            (
                "steering wheel, dashboard, instrument cluster, A-pillar, door frame, "
                "mirror housing, mirror frame, mirror mount"
            ),
        )
    if processing_mode == "opening_background":
        prompts = {
            "trunk-open": (
                "outdoor background visible around the vehicle and through the open "
                "trunk opening"
            ),
            "driver-entry": "outdoor background and ground visible through the open driver door",
            "driver-door": (
                "window glass, outdoor background and ground visible around the driver door"
            ),
            "passenger-entry": (
                "outdoor background and ground visible through the open passenger door"
            ),
            "passenger-door": (
                "window glass, outdoor background and ground visible around the passenger door"
            ),
            "driver-door-open": (
                "outdoor background and ground visible around and through the open driver door"
            ),
            "passenger-door-open": (
                "outdoor background and ground visible around and through the open passenger door"
            ),
        }
        return (
            prompts.get(
                orientation_key,
                "outdoor background visible through the open vehicle door",
            ),
            (
                "vehicle body, open door, open tailgate, cargo area, seats, dashboard, "
                "pillars, trim, mirror housings, mirror frames, mirror mounts"
            ),
        )
    prompts = {
        "front-interior": "windshield and side window glass",
        "rear-row-driver": "side window and rear window glass",
        "rear-row-passenger": "side window and rear window glass",
        "panoramic-roof": "panoramic glass roof and window glass",
    }
    return (
        prompts.get(orientation_key, WINDOW_MASK_PROMPT),
        (
            "dashboard, seats, steering wheel, instrument cluster, pillars, door frame, "
            "mirror housings, mirror frames, mirror mounts, interior trim"
        ),
    )


@dataclass(frozen=True)
class StandardOrientation:
    key: str
    name: str
    instruction: str
    category: str
    processing_mode: str
    required: bool = True
    repeatable: bool = False
    default_instances: int = 1
    max_instances: int = 1

    @property
    def requires_processing(self) -> bool:
        return self.processing_mode in PROCESSING_REQUIRED_MODES


STANDARD_ORIENTATIONS = [
    StandardOrientation(
        "front",
        "Vorne",
        "Fahrzeug gerade und vollständig von vorne aufnehmen.",
        "exterior",
        "optimized",
    ),
    StandardOrientation(
        "front-left",
        "Vorne links",
        "Vordere linke Fahrzeugecke vollständig zeigen.",
        "exterior",
        "optimized",
    ),
    StandardOrientation(
        "front-lower-left",
        "Vorderes unten links zugeschnitten",
        "Vorderen unteren Fahrzeugbereich von links aufnehmen.",
        "exterior",
        "optimized",
    ),
    StandardOrientation(
        "left",
        "Links",
        "Linke Fahrzeugseite gerade und vollständig aufnehmen.",
        "exterior",
        "optimized",
    ),
    StandardOrientation(
        "rear-left",
        "Hinten links",
        "Hintere linke Fahrzeugecke vollständig zeigen.",
        "exterior",
        "optimized",
    ),
    StandardOrientation(
        "rear",
        "Hinten",
        "Fahrzeug gerade und vollständig von hinten aufnehmen.",
        "exterior",
        "optimized",
    ),
    StandardOrientation(
        "rear-right",
        "Hinten rechts",
        "Hintere rechte Fahrzeugecke vollständig zeigen.",
        "exterior",
        "optimized",
    ),
    StandardOrientation(
        "right",
        "Rechts",
        "Rechte Fahrzeugseite gerade und vollständig aufnehmen.",
        "exterior",
        "optimized",
    ),
    StandardOrientation(
        "front-right",
        "Vorne rechts",
        "Vordere rechte Fahrzeugecke vollständig zeigen.",
        "exterior",
        "optimized",
    ),
    StandardOrientation(
        "front-lower-right",
        "Vorderes unten rechts zugeschnitten",
        "Vorderen unteren Fahrzeugbereich von rechts aufnehmen.",
        "exterior",
        "optimized",
    ),
    StandardOrientation(
        "driver-entry",
        "Einstieg Fahrer",
        "Seitlichen Einblick durch die geöffnete Fahrertür aufnehmen.",
        "interior",
        "opening_background",
    ),
    StandardOrientation(
        "steering-wheel",
        "Lenkrad",
        "Lenkrad mittig und ohne Spiegelungen aufnehmen.",
        "interior",
        "window_background",
    ),
    StandardOrientation(
        "instruments",
        "Instrumente",
        "Instrumente vollständig, gerade und scharf aufnehmen.",
        "interior",
        "original",
    ),
    StandardOrientation(
        "driver-door",
        "Türe Fahrerseite",
        "Innenseite der Fahrertür vollständig aufnehmen.",
        "interior",
        "opening_background",
    ),
    StandardOrientation(
        "rear-row-driver",
        "Hintere Reihe Fahrer",
        "Rücksitzbereich von der Fahrerseite aufnehmen.",
        "interior",
        "window_background",
    ),
    StandardOrientation(
        "front-interior",
        "Innenansicht vorne",
        "Gesamteindruck des vorderen Innenraums aufnehmen.",
        "interior",
        "window_background",
    ),
    StandardOrientation(
        "center-console",
        "Mittelkonsole",
        "Mittelkonsole vollständig und scharf aufnehmen.",
        "interior",
        "original",
    ),
    StandardOrientation(
        "infotainment",
        "Navigation/Infotainment",
        "Gewünschte Ansicht des Infotainment-Systems aufnehmen.",
        "interior",
        "original",
        repeatable=True,
        default_instances=1,
        max_instances=5,
    ),
    StandardOrientation(
        "passenger-door",
        "Türe Beifahrerseite",
        "Innenseite der Beifahrertür vollständig aufnehmen.",
        "interior",
        "opening_background",
    ),
    StandardOrientation(
        "passenger-entry",
        "Einstieg Beifahrer",
        "Seitlichen Einblick durch die geöffnete Beifahrertür aufnehmen.",
        "interior",
        "opening_background",
    ),
    StandardOrientation(
        "driver-door-open",
        "Fahrertür geöffnet",
        "Fahrzeug mit geöffneter Fahrertür aufnehmen.",
        "interior",
        "opening_background",
    ),
    StandardOrientation(
        "passenger-door-open",
        "Beifahrertür geöffnet",
        "Fahrzeug mit geöffneter Beifahrertür aufnehmen.",
        "interior",
        "opening_background",
    ),
    StandardOrientation(
        "rear-row-passenger",
        "Hintere Reihe Beifahrer",
        "Rücksitzbereich von der Beifahrerseite aufnehmen.",
        "interior",
        "window_background",
    ),
    StandardOrientation(
        "trunk-open",
        "Kofferraum offen",
        "Geöffneten Kofferraum vollständig aufnehmen.",
        "interior",
        "opening_background",
    ),
    StandardOrientation(
        "engine-bay",
        "Motorraum",
        "Geöffneten Motorraum vollständig aufnehmen.",
        "detail",
        "configurable",
    ),
    StandardOrientation(
        "wheel-front-left",
        "Reifen vorne links",
        "Vorderrad auf der Fahrerseite vollständig aufnehmen.",
        "detail",
        "optimized",
    ),
    StandardOrientation(
        "wheel-front-right",
        "Reifen vorne rechts",
        "Vorderrad auf der Beifahrerseite vollständig aufnehmen.",
        "detail",
        "optimized",
    ),
    StandardOrientation(
        "wheel-rear-left",
        "Reifen hinten links",
        "Hinterrad auf der Fahrerseite vollständig aufnehmen.",
        "detail",
        "optimized",
    ),
    StandardOrientation(
        "wheel-rear-right",
        "Reifen hinten rechts",
        "Hinterrad auf der Beifahrerseite vollständig aufnehmen.",
        "detail",
        "optimized",
    ),
    StandardOrientation(
        "panoramic-roof",
        "Panorama-Schiebedach",
        "Panorama-Schiebedach aus dem Innenraum aufnehmen.",
        "interior",
        "optimized",
    ),
    StandardOrientation(
        "windshield",
        "Windschutzscheibe",
        "Windschutzscheibe vollständig und reflexionsarm aufnehmen.",
        "detail",
        "original",
    ),
    StandardOrientation(
        "tire-tread",
        "Reifenprofil",
        "Reifenprofil gut erkennbar und scharf aufnehmen.",
        "detail",
        "original",
        required=False,
        repeatable=True,
        default_instances=1,
        max_instances=4,
    ),
    StandardOrientation(
        "odometer",
        "Kilometerstand",
        "Kilometerstand lesbar und scharf aufnehmen.",
        "detail",
        "original",
    ),
    StandardOrientation(
        "key",
        "Schlüssel",
        "Alle zum Fahrzeug gehörenden Schlüssel aufnehmen.",
        "detail",
        "original",
    ),
    StandardOrientation(
        "damage",
        "Schaden",
        "Schaden mit ausreichend Kontext und zusätzlich im Detail aufnehmen.",
        "detail",
        "original",
        required=False,
        repeatable=True,
        default_instances=1,
        max_instances=20,
    ),
    StandardOrientation(
        "special",
        "Spezialaufnahme",
        "Zusätzliche frei wählbare Fahrzeugaufnahme erstellen.",
        "special",
        "original",
        required=False,
        repeatable=True,
        default_instances=1,
        max_instances=50,
    ),
]

STANDARD_ORIENTATION_KEYS = frozenset(item.key for item in STANDARD_ORIENTATIONS)


def default_silhouette_path(key: str | None) -> str | None:
    if key not in STANDARD_ORIENTATION_KEYS:
        return None
    return f"/orientation-guides/{key}.png"


def instance_name(name: str, instance_index: int, repeatable: bool) -> str:
    if not repeatable:
        return name
    suffix = f" {instance_index}"
    return f"{name[: 160 - len(suffix)]}{suffix}"
