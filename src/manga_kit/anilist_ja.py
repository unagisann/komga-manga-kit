"""Reviewed Japanese labels for AniList's closed genre and tag vocabulary.

AniList genres and tags are a fixed list, so one reviewed table replaces any
per-series translation. Adult tags and the Hentai genre are intentionally
absent; tags missing from this table keep their AniList spelling.
Komga lowercases genres and tags, so Latin-only labels are avoided.
"""

# Excluded from candidates regardless of other flags.
EXCLUDED_GENRES = {"Hentai"}
EXCLUDED_TAG_CATEGORIES = {"Sexual Content"}

GENRES = {
    "Action": "アクション", "Adventure": "冒険", "Comedy": "コメディ", "Drama": "ドラマ",
    "Ecchi": "お色気", "Fantasy": "ファンタジー", "Horror": "ホラー", "Mahou Shoujo": "魔法少女",
    "Mecha": "メカ", "Music": "音楽", "Mystery": "ミステリー", "Psychological": "心理",
    "Romance": "恋愛", "Sci-Fi": "サイエンスフィクション", "Slice of Life": "日常", "Sports": "スポーツ",
    "Supernatural": "超常", "Thriller": "サスペンス",
}

# AniList's Demographic tags become genres, the same place as 少年・青年 in Komga.
DEMOGRAPHICS = {"Shounen": "少年", "Seinen": "青年", "Shoujo": "少女", "Josei": "レディース", "Kids": "児童"}

TAGS = {
    # Cast - Main Cast
    "Anti-Hero": "アンチヒーロー", "Elderly Protagonist": "高齢の主人公", "Ensemble Cast": "群像劇",
    "Estranged Family": "家族の不和", "Female Protagonist": "女性主人公", "Male Protagonist": "男性主人公",
    "Primarily Adult Cast": "大人中心", "Primarily Animal Cast": "動物中心", "Primarily Child Cast": "子ども中心",
    "Primarily Female Cast": "女性中心", "Primarily Male Cast": "男性中心", "Primarily Teen Cast": "10代中心",
    # Cast - Traits
    "Age Regression": "若返り", "Agender": "アジェンダー", "Aliens": "宇宙人", "Amnesia": "記憶喪失",
    "Angels": "天使", "Anthropomorphism": "擬人化", "Aromantic": "アロマンティック", "Arranged Marriage": "政略結婚",
    "Artificial Intelligence": "人工知能", "Asexual": "アセクシュアル", "Bisexual": "バイセクシュアル",
    "Butler": "執事", "Centaur": "ケンタウロス", "Chimera": "キメラ", "Chuunibyou": "中二病", "Clone": "クローン",
    "Cosplay": "コスプレ", "Cowboys": "カウボーイ", "Crossdressing": "異性装", "Cyborg": "サイボーグ",
    "Delinquents": "不良", "Demons": "悪魔", "Detective": "探偵", "Dinosaurs": "恐竜", "Disability": "障がい",
    "Dissociative Identities": "多重人格", "Dragons": "ドラゴン", "Dullahan": "デュラハン", "Elf": "エルフ",
    "Fairy": "妖精", "Femboy": "男の娘", "Ghost": "幽霊", "Goblin": "ゴブリン", "Gods": "神様", "Gyaru": "ギャル",
    "Hikikomori": "引きこもり", "Homeless": "ホームレス", "Idol": "アイドル", "Kemonomimi": "けもみみ",
    "Kuudere": "クーデレ", "Maids": "メイド", "Mermaid": "人魚", "Monster Boy": "人外男子",
    "Monster Girl": "人外娘", "Nekomimi": "猫耳", "Ninja": "忍者", "Nudity": "裸体描写", "Nun": "修道女",
    "Office Lady": "働く女性", "Oiran": "花魁", "Ojou-sama": "お嬢様", "Orphan": "孤児", "Pirates": "海賊",
    "Robots": "ロボット", "Samurai": "侍", "Shrine Maiden": "巫女", "Skeleton": "骸骨", "Succubus": "サキュバス",
    "Tanned Skin": "日焼け肌", "Teacher": "教師", "Tomboy": "ボーイッシュ", "Transgender": "トランスジェンダー",
    "Tsundere": "ツンデレ", "Twins": "双子", "Vampire": "吸血鬼", "Veterinarian": "獣医", "Vikings": "ヴァイキング",
    "Villainess": "悪役令嬢", "VTuber": "ブイチューバー", "Werewolf": "人狼", "Witch": "魔女",
    "Yandere": "ヤンデレ", "Zombie": "ゾンビ",
    # Setting - Scene
    "Bar": "バー", "Boarding School": "全寮制学校", "Camping": "キャンプ", "Circus": "サーカス",
    "Coastal": "海辺", "College": "大学", "Desert": "砂漠", "Dungeon": "ダンジョン", "Foreign": "異国",
    "Inn": "宿屋", "Konbini": "コンビニ", "Middle East": "中東", "Natural Disaster": "自然災害",
    "Office": "オフィス", "Outdoor Activities": "アウトドア", "Prison": "刑務所", "Restaurant": "飲食店",
    "Rural": "田舎", "School": "学校", "School Club": "部活", "Snowscape": "雪景色", "Urban": "都会",
    "Wilderness": "荒野", "Work": "仕事",
    # Setting - Time / Universe
    "Achronological Order": "時系列シャッフル", "Anachronism": "時代錯誤", "Ancient China": "古代中国",
    "Dystopian": "ディストピア", "Historical": "歴史", "Medieval": "中世", "Time Skip": "タイムスキップ",
    "Afterlife": "死後の世界", "Alternate Universe": "パラレルワールド", "Augmented Reality": "拡張現実",
    "Post-Apocalyptic": "終末世界", "Space": "宇宙", "Urban Fantasy": "現代ファンタジー",
    "Virtual World": "仮想世界",
    # Technical
    "4-koma": "4コマ", "Achromatic": "モノクロ", "Advertisement": "広告", "Anthology": "アンソロジー",
    "CGI": "コンピューターグラフィックス", "Episodic": "一話完結", "Flash": "フラッシュアニメ", "Full CGI": "フルCG",
    "Full Color": "フルカラー", "Gekiga": "劇画", "Graduation Project": "卒業制作", "Long Strip": "縦読み",
    "Mixed Media": "ミクストメディア", "No Dialogue": "セリフなし", "Non-fiction": "ノンフィクション",
    "POV": "一人称視点", "Puppetry": "人形劇", "Rotoscoping": "ロトスコープ", "Short-Form Chapter": "ショート",
    "Stop Motion": "ストップモーション", "Vertical Video": "縦型動画",
    # Theme - Action
    "Archery": "弓術", "Battle Royale": "バトルロイヤル", "Espionage": "スパイ", "Fugitive": "逃亡者",
    "Guns": "銃", "Martial Arts": "格闘技", "Spearplay": "槍術", "Swordplay": "剣術",
    # Theme - Arts
    "Acting": "演技", "Ballet": "バレエ", "Calligraphy": "書道", "Classic Literature": "古典文学",
    "Drawing": "絵画", "Fashion": "ファッション", "Food": "グルメ", "Kabuki": "歌舞伎", "Makeup": "メイク",
    "Manzai": "漫才", "Modeling": "モデル", "Photography": "写真", "Rakugo": "落語", "Writing": "執筆",
    "Band": "バンド", "Classical Music": "クラシック音楽", "Dancing": "ダンス", "Hip-hop Music": "ヒップホップ",
    "Jazz Music": "ジャズ", "Metal Music": "メタル", "Musical Theater": "ミュージカル", "Rock Music": "ロック",
    # Theme - Comedy / Drama
    "Parody": "パロディ", "Satire": "風刺", "Slapstick": "ドタバタ", "Surreal Comedy": "シュール",
    "Bullying": "いじめ", "Class Struggle": "階級闘争", "Coming of Age": "青春", "Conspiracy": "陰謀",
    "Eco-Horror": "エコホラー", "Fake Relationship": "偽装恋愛", "Kingdom Management": "領地経営",
    "Rehabilitation": "更生", "Revenge": "復讐", "Suicide": "自殺", "Tragedy": "悲劇",
    # Theme - Fantasy
    "Alchemy": "錬金術", "Body Swapping": "入れ替わり", "Cultivation": "修仙", "Curses": "呪い",
    "Exorcism": "除霊", "Fairy Tale": "童話", "Henshin": "変身ヒーロー", "Isekai": "異世界", "Kaiju": "怪獣",
    "Magic": "魔法", "Mythology": "神話", "Necromancy": "死霊術", "Reverse Isekai": "逆異世界",
    "Shapeshifting": "変身能力", "Steampunk": "スチームパンク", "Super Power": "超能力",
    "Superhero": "スーパーヒーロー", "Wuxia": "武侠", "Youkai": "妖怪",
    # Theme - Game
    "Board Game": "ボードゲーム", "E-Sports": "eスポーツ", "Video Games": "ゲーム",
    "Card Battle": "カードバトル", "Go": "囲碁", "Karuta": "かるた", "Mahjong": "麻雀", "Poker": "ポーカー",
    "Shogi": "将棋",
    "Acrobatics": "アクロバット", "Airsoft": "サバイバルゲーム", "American Football": "アメフト",
    "Athletics": "陸上競技", "Badminton": "バドミントン", "Baseball": "野球", "Basketball": "バスケットボール",
    "Bowling": "ボウリング", "Boxing": "ボクシング", "Cheerleading": "チアリーディング", "Cycling": "自転車",
    "Fencing": "フェンシング", "Fishing": "釣り", "Fitness": "フィットネス", "Football": "サッカー",
    "Golf": "ゴルフ", "Handball": "ハンドボール", "Ice Sports": "氷上スポーツ", "Judo": "柔道",
    "Lacrosse": "ラクロス", "Parkour": "パルクール", "Rugby": "ラグビー", "Scuba Diving": "スキューバダイビング",
    "Skateboarding": "スケートボード", "Sumo": "相撲", "Surfing": "サーフィン", "Swimming": "水泳",
    "Table Tennis": "卓球", "Tennis": "テニス", "Volleyball": "バレーボール", "Wrestling": "レスリング",
    # Theme - Other
    "Adoption": "養子", "Animals": "動物", "Astronomy": "天文", "Autobiographical": "自伝",
    "Biographical": "伝記", "Blackmail": "脅迫", "Body Horror": "ボディホラー", "Body Image": "容姿",
    "Brainwashing": "洗脳", "Cannibalism": "食人", "Chibi": "ちびキャラ", "Cosmic Horror": "コズミックホラー",
    "Creature Taming": "テイム", "Crime": "犯罪", "Crossover": "クロスオーバー", "Death Game": "デスゲーム",
    "Denpa": "電波", "Drugs": "薬物", "Economics": "経済", "Educational": "学習", "Environmental": "環境",
    "Ero Guro": "エログロ", "Exiled": "追放", "Filmmaking": "映画制作", "Found Family": "疑似家族",
    "Gambling": "ギャンブル", "Gender Bending": "性転換", "Gore": "残酷描写",
    "Human Experimentation": "人体実験", "Indigenous Cultures": "先住民文化", "Language Barrier": "言葉の壁",
    "LGBTQ+ Themes": "性的少数者", "Lost Civilization": "失われた文明", "Marriage": "結婚",
    "Matriarchy": "女系社会", "Medicine": "医療", "Memory Manipulation": "記憶操作", "Meta": "メタ",
    "Mountaineering": "登山", "Noir": "ノワール", "Otaku Culture": "オタク文化", "Pandemic": "パンデミック",
    "Philosophy": "哲学", "Politics": "政治", "Pregnancy": "妊娠", "Prophecy": "予言",
    "Proxy Battle": "代理バトル", "Psychosexual": "性的心理", "Reincarnation": "転生", "Religion": "宗教",
    "Rescue": "救助", "Royal Affairs": "王室", "Slavery": "奴隷", "Software Development": "ソフトウェア開発",
    "Survival": "サバイバル", "Terrorism": "テロ", "Torture": "拷問", "Travel": "旅", "Vocal Synth": "音声合成",
    "War": "戦争",
    "Assassins": "暗殺者", "Criminal Organization": "犯罪組織", "Cult": "カルト", "Firefighters": "消防士",
    "Gangs": "ギャング", "Mafia": "マフィア", "Military": "軍隊", "Police": "警察", "Triads": "黒社会",
    "Yakuza": "ヤクザ",
    "Aviation": "航空", "Cars": "車", "Mopeds": "スクーター", "Motorcycles": "バイク", "Ships": "船",
    "Tanks": "戦車", "Trains": "鉄道",
    # Theme - Romance
    "Age Gap": "年の差", "Boys' Love": "ボーイズラブ", "Cheating": "浮気", "Cohabitation": "同居",
    "Female Harem": "ハーレム", "Heterosexual": "男女の恋愛", "Incest": "近親", "Inseki": "義理の家族",
    "Interspecies": "異種族恋愛", "Love Triangle": "三角関係", "Male Harem": "逆ハーレム",
    "Matchmaking": "お見合い", "Mixed Gender Harem": "混合ハーレム", "Polyamorous": "ポリアモリー",
    "Teens' Love": "ティーンズラブ", "Unrequited Love": "片思い", "Yuri": "百合",
    # Theme - Sci-Fi / Slice of Life
    "Cyberpunk": "サイバーパンク", "Space Opera": "スペースオペラ", "Time Loop": "タイムループ",
    "Time Manipulation": "時間操作", "Tokusatsu": "特撮", "Real Robot": "リアルロボット",
    "Super Robot": "スーパーロボット",
    "Agriculture": "農業", "Cute Boys Doing Cute Things": "かわいい男の子の日常",
    "Cute Girls Doing Cute Things": "かわいい女の子の日常", "Family Life": "家族", "Horticulture": "園芸",
    "Iyashikei": "癒し系", "Parenthood": "子育て",
}
