import aiohttp


from datetime import datetime


from .database import get_conn



VOCADB_API = "https://vocadb.net/api/songs"



async def fetch_songs(limit=50):


    params = {

        "sort": "RatingScore",

        "maxResults": limit,

        "fields": "Albums,Artists,Tags"

    }


    headers = {

        "User-Agent": "AstrBot-Daily-Vocaloid/1.0"

    }


    try:


        timeout = aiohttp.ClientTimeout(

            total=20

        )


        async with aiohttp.ClientSession(

            timeout=timeout

        ) as session:


            async with session.get(

                VOCADB_API,

                params=params,

                headers=headers

            ) as resp:


                if resp.status != 200:

                    print(

                        f"VocaDB HTTP Error: {resp.status}"

                    )

                    return []


                data = await resp.json()


                return data.get(

                    "items",

                    []

                )



    except Exception as e:


        print(

            f"VocaDB Request Error: {e}"

        )


        return []



def save_vocadb_songs(items):


    conn = get_conn()


    cur = conn.cursor()



    for item in items:



        song_id = str(

            item.get("id")

        )



        title = item.get(

            "name",

            ""

        )



        score = item.get(

            "ratingScore",

            0

        )



        artists = item.get(

            "artists",

            []

        )



        producer = ""


        vocal = ""



        if artists:


            names = []


            for artist in artists:


                name = artist.get(

                    "name",

                    ""

                )


                if name:

                    names.append(name)



            if names:


                producer = ", ".join(

                    names

                )



        cur.execute(

            """

            INSERT OR IGNORE INTO songs

            (

                vocadb_id,

                title_jp,

                title_zh,

                producer,

                vocal,

                publish_date,

                rating_score,

                pool_type,

                available,

                last_checked_at

            )

            VALUES

            (

                ?,?,?,?,?,?,?,?,?,?

            )

            """,

            (

                song_id,

                title,

                "",

                producer,

                vocal,

                "",

                score,

                "normal",

                1,

                datetime.now().isoformat()

            )

        )



    conn.commit()


    conn.close()




async def sync_vocadb():


    songs = await fetch_songs()



    if not songs:


        return False



    save_vocadb_songs(

        songs

    )



    return True
