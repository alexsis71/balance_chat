# language: ru

Функция: Последовательные deterministic PATCH по периоду, GEO и business entity

  @BC-01 @P0 @current-contract @PR4 @transition @periods @geo @business
  Сценарий: Ростов май затем апрель затем Самара затем Ухта затем май
    Когда T1 пользователь спрашивает "Покажи распределение газа в Ростовскую область за май 2025"
    Тогда T1 имеет operation "show", metric "distribution" и GEO "Ростовская область"
    И T1 имеет период [2025-05-01, 2025-06-01) и revision 1
    Когда T2 пользователь спрашивает "А за апрель?"
    Тогда T2 имеет operation "show", metric "distribution" и GEO "Ростовская область"
    И T2 имеет период [2025-04-01, 2025-05-01) и revision 2
    Когда T3 пользователь спрашивает "А по Самарской области?"
    Тогда T3 имеет operation "show", metric "distribution" и GEO "Самарская область"
    И T3 имеет период [2025-04-01, 2025-05-01) и revision 3
    Когда T4 пользователь спрашивает "Только из ГП ТГ Ухта"
    Тогда T4 имеет operation "show", metric "distribution", source business "ГП ТГ Ухта" и GEO "Самарская область"
    И T4 имеет период [2025-04-01, 2025-05-01) и revision 4
    Когда T5 пользователь спрашивает "А за май?"
    Тогда T5 имеет operation "show", metric "distribution", source business "ГП ТГ Ухта" и GEO "Самарская область"
    И T5 имеет период [2025-05-01, 2025-06-01) и revision 5

  @BC-02 @P0 @current-contract @PR4 @transition @aliases @homonym
  Сценарий: Ухта затем Самара затем апрель без смешения business и GEO
    Когда T1 пользователь спрашивает "Покажи распределение газа в Ростовскую область за май 2025"
    Тогда T1 имеет operation "show", metric "distribution" и GEO "Ростовская область"
    И T1 имеет период [2025-05-01, 2025-06-01) и revision 1
    Когда T2 пользователь спрашивает "Только из ТГ Ухта"
    Тогда T2 имеет operation "show", metric "distribution", source business "ГП ТГ Ухта" и GEO "Ростовская область"
    И T2 имеет период [2025-05-01, 2025-06-01) и revision 2
    Когда T3 пользователь спрашивает "А по Самарской области?"
    Тогда T3 имеет operation "show", metric "distribution", source business "ГП ТГ Ухта" и GEO "Самарская область"
    И T3 имеет период [2025-05-01, 2025-06-01) и revision 3
    Когда T4 пользователь спрашивает "А за апрель?"
    Тогда T4 имеет operation "show", metric "distribution", source business "ГП ТГ Ухта" и GEO "Самарская область"
    И T4 имеет период [2025-04-01, 2025-05-01) и revision 4
