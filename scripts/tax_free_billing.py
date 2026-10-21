"""五个免税州的地址素材，与 apps/web/src/card-payment/billingExamples.ts 同源。

由脚本从该 TS 文件自动生成，改动前端数据后请重新生成。
"""

import random

PLACES = {
    "OR": [
        {"city": "Portland", "zips": ['97201', '97204', '97205', '97209', '97210', '97211', '97212', '97214', '97215', '97217', '97219', '97225', '97227', '97232', '97239'], "streets": ['NE Alberta St', 'SE Hawthorne Blvd', 'NW 23rd Ave', 'SW Barbur Blvd', 'NE Sandy Blvd', 'SE Division St', 'N Mississippi Ave', 'NW Lovejoy St', 'SE Belmont St', 'NE Broadway', 'SW Morrison St', 'SE Powell Blvd', 'NE Glisan St', 'NW Thurman St', 'SE Clinton St', 'NE Skidmore St']},
        {"city": "Eugene", "zips": ['97401', '97402', '97403', '97404', '97405'], "streets": ['Willamette St', 'E 13th Ave', 'W 7th Ave', 'Coburg Rd', 'Franklin Blvd', 'Hilyard St', 'Chambers St', 'Barger Dr']},
        {"city": "Salem", "zips": ['97301', '97302', '97303', '97304', '97305'], "streets": ['Commercial St SE', 'State St', 'Center St NE', 'Lancaster Dr SE', 'Market St NE', 'Hawthorne Ave SE', 'Liberty St S']},
        {"city": "Bend", "zips": ['97701', '97702', '97703'], "streets": ['NW Wall St', 'SW Century Dr', 'NE 3rd St', 'SW Highland Ave', 'NW Franklin Ave', 'SE Reed Market Rd']},
        {"city": "Beaverton", "zips": ['97005', '97006', '97007', '97008'], "streets": ['SW Broadway St', 'SW Canyon Rd', 'SW Hall Blvd', 'SW Allen Blvd', 'SW Watson Ave', 'SW Farmington Rd']},
        {"city": "Corvallis", "zips": ['97330', '97331', '97333'], "streets": ['NW Monroe Ave', 'SW 3rd St', 'NW Van Buren Ave', 'SW Philomath Blvd', 'NW Harrison Blvd']},
    ],
    "MT": [
        {"city": "Billings", "zips": ['59101', '59102', '59105', '59106'], "streets": ['Grand Ave', 'Central Ave', 'Poly Dr', 'Rimrock Rd', '24th St W', 'Monad Rd', 'Broadwater Ave', 'King Ave W']},
        {"city": "Missoula", "zips": ['59801', '59802', '59803', '59808'], "streets": ['Higgins Ave', 'Brooks St', 'Reserve St', 'South Ave', 'Stephens Ave', 'Ryman St', 'S 3rd St W']},
        {"city": "Bozeman", "zips": ['59715', '59718'], "streets": ['W Main St', 'N 19th Ave', 'W Babcock St', 'S 11th Ave', 'W Olive St', 'Ferguson Ave']},
        {"city": "Helena", "zips": ['59601', '59602'], "streets": ['N Last Chance Gulch', 'Euclid Ave', 'Prospect Ave', 'Montana Ave', 'N Benton Ave', 'Saddle Dr']},
        {"city": "Great Falls", "zips": ['59401', '59404', '59405'], "streets": ['Central Ave', '10th Ave S', '3rd St NW', 'Fox Farm Rd', '2nd Ave N', 'Smelter Ave NE']},
        {"city": "Kalispell", "zips": ['59901'], "streets": ['S Main St', 'E Idaho St', 'N Meridian Rd', 'US Highway 93 S', 'W Reserve Dr']},
    ],
    "DE": [
        {"city": "Wilmington", "zips": ['19801', '19803', '19805', '19806', '19808', '19809', '19810'], "streets": ['N Market St', 'Delaware Ave', 'Concord Pike', 'Kirkwood Hwy', 'Lancaster Ave', 'Foulk Rd', 'N Union St', 'Pennsylvania Ave', 'W 4th St']},
        {"city": "Dover", "zips": ['19901', '19904'], "streets": ['S State St', 'N DuPont Hwy', 'Loockerman St', 'S Governors Ave', 'W Division St', 'Hazel Rd']},
        {"city": "Newark", "zips": ['19711', '19713'], "streets": ['E Main St', 'Elkton Rd', 'S College Ave', 'Cleveland Ave', 'New London Rd', 'Paper Mill Rd']},
        {"city": "Middletown", "zips": ['19709'], "streets": ['N Broad St', 'W Main St', 'E Lake St', 'Levels Rd', 'W Commerce St']},
        {"city": "Smyrna", "zips": ['19977'], "streets": ['S Dupont Blvd', 'E Glenwood Ave', 'N Main St', 'Wheatleys Pond Rd']},
        {"city": "Milford", "zips": ['19963'], "streets": ['N Walnut St', 'SE Front St', 'Milford Harrington Hwy', 'S Dupont Blvd', 'Causey Ave']},
    ],
    "NH": [
        {"city": "Manchester", "zips": ['03101', '03102', '03103', '03104', '03109'], "streets": ['Elm St', 'S Willow St', 'Hanover St', 'Mammoth Rd', 'Beech St', 'Second St', 'Union St', 'Bridge St', 'Chestnut St']},
        {"city": "Nashua", "zips": ['03060', '03062', '03063', '03064'], "streets": ['Main St', 'Amherst St', 'Daniel Webster Hwy', 'E Dunstable Rd', 'Broad St', 'Concord St', 'Exit 5 Dr']},
        {"city": "Concord", "zips": ['03301', '03303'], "streets": ['N Main St', 'Loudon Rd', 'S Main St', 'Pleasant St', 'Stickney Ave', 'Hall St']},
        {"city": "Portsmouth", "zips": ['03801'], "streets": ['Congress St', 'Islington St', 'Lafayette Rd', 'Woodbury Ave', 'Maplewood Ave', 'Middle St']},
        {"city": "Dover", "zips": ['03820'], "streets": ['Central Ave', 'Locust St', 'Silver St', 'Washington St', 'Cocheco St']},
        {"city": "Keene", "zips": ['03431'], "streets": ['Main St', 'Winchester St', 'West St', 'Court St', 'Emerald St']},
        {"city": "Salem", "zips": ['03079'], "streets": ['S Broadway', 'N Broadway', 'Main St', 'Industrial Dr', 'Cluff Crossing Rd']},
    ],
    "AK": [
        {"city": "Anchorage", "zips": ['99501', '99502', '99503', '99504', '99507', '99508', '99515', '99516', '99517', '99518'], "streets": ['W 4th Ave', 'E Northern Lights Blvd', 'Spenard Rd', 'Minnesota Dr', 'Tudor Rd', 'Abbott Rd', 'Huffman Rd', 'Lake Otis Pkwy', 'C St', 'Boniface Pkwy', 'Muldoon Rd']},
        {"city": "Fairbanks", "zips": ['99701', '99709', '99712'], "streets": ['Airport Way', 'College Rd', 'Johansen Expy', 'Cushman St', 'Barnette St', 'Peger Rd']},
        {"city": "Juneau", "zips": ['99801'], "streets": ['Egan Dr', 'Glacier Hwy', 'Mendenhall Loop Rd', 'Douglas Hwy', 'Channel Dr']},
        {"city": "Wasilla", "zips": ['99654'], "streets": ['E Parks Hwy', 'N Main St', 'S Knik Goose Bay Rd', 'E Palmer Wasilla Hwy']},
        {"city": "Eagle River", "zips": ['99577'], "streets": ['Old Glenn Hwy', 'E Eagle River Loop Rd', 'Business Blvd', 'Snowshoe Cir']},
        {"city": "Sitka", "zips": ['99835'], "streets": ['Lincoln St', 'Harbor Dr', 'Halibut Point Rd', 'Sawmill Creek Rd']},
    ],
}

TAX_FREE_STATES = list(PLACES)


def random_tax_free_billing(email: str = "", name: str = "") -> dict:
    """与前端 randomBillingExample 等价：随机州 + 随机城市/街道/邮编 + 随机门牌号。"""
    state = random.choice(TAX_FREE_STATES)
    place = random.choice(PLACES[state])
    street = random.choice(place["streets"])
    zipcode = random.choice(place["zips"])
    number = random.randint(100, 8999)
    unit = f" Apt {random.randint(1, 40)}" if random.random() < 0.25 else ""
    return {
        "name": name,
        "email": email,
        "line1": f"{number} {street}{unit}",
        "city": place["city"],
        "state": state,
        "postal_code": zipcode,
        "country": "US",
    }
