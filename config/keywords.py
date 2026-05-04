"""Such-Konfiguration: Include-Keywords, Exclude-Keywords, Zielstädte."""

INCLUDE_QUERIES = [
    "Bürokraft",
    "Sachbearbeitung",
    "Verwaltungsmitarbeiter",
    "Office Manager",
    "Assistenz der Geschäftsführung",
    "Teamassistenz",
    "Sekretariat",
    "Empfangskraft",
    "Backoffice",
    "allgemeine Verwaltung",
]

EXCLUDE_TERMS = [
    "Buchhaltung",
    "Finanzbuchhaltung",
    "Lohnabrechnung",
    "Bilanzbuchhalter",
    "Steuerfachangestellte",
    "Steuerfachangestellter",
    "Controlling",
    "Debitorenbuchhalter",
    "Kreditorenbuchhalter",
    "Bilanzbuchhaltung",
    "Lohnbuchhaltung",
    "Buchhalter",
    "Buchhalterin",
]

# Top 21–100 deutsche Städte nach Einwohnerzahl (2024).
# Quelle: Statistisches Bundesamt / Wikipedia "Liste der Großstädte in Deutschland".
TARGET_CITIES = [
    "Mannheim", "Karlsruhe", "Augsburg", "Wiesbaden", "Mönchengladbach",
    "Gelsenkirchen", "Aachen", "Braunschweig", "Chemnitz", "Kiel",
    "Halle", "Magdeburg", "Freiburg", "Krefeld", "Lübeck",
    "Mainz", "Erfurt", "Oberhausen", "Rostock", "Kassel",
    "Hagen", "Hamm", "Saarbrücken", "Mülheim", "Potsdam",
    "Ludwigshafen", "Oldenburg", "Leverkusen", "Osnabrück", "Solingen",
    "Heidelberg", "Herne", "Neuss", "Darmstadt", "Paderborn",
    "Regensburg", "Ingolstadt", "Würzburg", "Fürth", "Wolfsburg",
    "Offenbach", "Ulm", "Heilbronn", "Pforzheim", "Göttingen",
    "Bottrop", "Trier", "Recklinghausen", "Reutlingen", "Bremerhaven",
    "Koblenz", "Bergisch Gladbach", "Jena", "Remscheid", "Erlangen",
    "Moers", "Siegen", "Hildesheim", "Salzgitter", "Cottbus",
    "Kaiserslautern", "Gütersloh", "Schwerin", "Witten", "Gera",
    "Iserlohn", "Esslingen", "Ludwigsburg", "Hanau", "Zwickau",
    "Düren", "Tübingen", "Ratingen", "Flensburg", "Lünen",
    "Villingen-Schwenningen", "Konstanz", "Worms", "Marl", "Velbert",
]

# Top-20 ausschließen (wenn Standort eindeutig zugeordnet werden kann)
EXCLUDED_CITIES = [
    "Berlin", "Hamburg", "München", "Köln", "Frankfurt am Main", "Frankfurt",
    "Stuttgart", "Düsseldorf", "Leipzig", "Dortmund", "Essen", "Bremen",
    "Dresden", "Hannover", "Nürnberg", "Duisburg", "Bochum", "Wuppertal",
    "Bielefeld", "Bonn", "Münster",
]
