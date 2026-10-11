# Relevance rubric for the match precision check

A pair of one organisation and one consultation is **relevant** when the consultation's
subject falls within the organisation's stated goals and declared interests in the EU
Transparency Register, so that an organisation of that kind would plausibly answer the
consultation on its own initiative.

Rules:

1. Judge only from the register text (goals, declared interest areas, category) and the
   consultation text (title and summary) given. Do not use anything else you may know
   about the organisation or the file.
2. A long list of ticked interest areas alone does not make a narrow or technical act
   relevant. The subject has to connect to what the goals say the organisation does.
3. A large horizontal file (public procurement, taxation, a general digital or AI act, a
   trade instrument) counts as relevant for a general business federation, a general
   public affairs consultancy or a broad policy think tank, because such files are their
   bread and butter. It does not count for a single-issue organisation unless its goals
   name that field.
4. A borderline case is **no**. Answer yes only when the link is plain from the texts.

Examples of **yes**:

- A fishers' trade union and a consultation on the training and certification of fishers.
- A consumer organisation and a consultation on unfair terms in consumer contracts.
- A network of cities and a strategy on urban mobility and public transport.

Examples of **no**:

- An environmental NGO and protective measures against plant pests in agriculture.
- A chemicals industry council and a strategy on the rights of victims of crime.
- A single-issue digital rights group and a revision of fisheries quota rules, even if
  "Maritime affairs" is among its ticked interests.

Output: `yes` or `no`, then one sentence saying which part of the goals or interests the
consultation does or does not connect to.
