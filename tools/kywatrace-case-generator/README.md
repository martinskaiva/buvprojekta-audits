# KywaTrace Case Example Generator

Neliels Streamlit rīks, kas pārveido būvprojekta audita fragmentu vienotā KywaTrace LinkedIn vizuālī.

## Funkcijas

- attēla augšupielāde;
- apgriešana;
- līdz 5 anonimizācijas zonām;
- blackout vai blur režīms;
- neatbilstības vietas izcelšana;
- auditorijas izvēle: attīstītājs / projektētājs / būvnieks;
- "Pasūtītāja prasība" pret "Projektā" salīdzinājums;
- 1080 × 1350 LinkedIn PNG eksports;
- neobligāts KywaTrace logo.

## Palaišana lokāli

```bash
cd tools/kywatrace-case-generator
pip install -r requirements.txt
streamlit run app.py
```

## Streamlit Community Cloud

Norādi:
- Repository: `martinskaiva/buvprojekta-audits`
- Branch: `main`
- Main file path: `tools/kywatrace-case-generator/app.py`

## Svarīgi

Rīks palīdz anonimizēt attēlu, bet automātiski negarantē, ka visi projekta identifikatori ir noņemti. Pirms publicēšanas vienmēr vizuāli pārbaudi gala PNG.
