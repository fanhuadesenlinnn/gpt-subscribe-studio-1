import type { BillingInput } from "../api/client";

/**
 * Real US cities, ZIP codes and street names in the five states without a
 * statewide sales tax (AK, DE, MT, NH, OR). Street names are genuine; the house
 * number is generated, so a filled result is a plausible address rather than a
 * verified deliverable one.
 */
interface Place {
  city: string;
  zips: readonly string[];
  streets: readonly string[];
}

const PLACES: Record<string, readonly Place[]> = {
  OR: [
    { city: "Portland", zips: ["97201", "97204", "97205", "97209", "97210", "97211", "97212", "97214", "97215", "97217", "97219", "97225", "97227", "97232", "97239"],
      streets: ["NE Alberta St", "SE Hawthorne Blvd", "NW 23rd Ave", "SW Barbur Blvd", "NE Sandy Blvd", "SE Division St", "N Mississippi Ave", "NW Lovejoy St", "SE Belmont St", "NE Broadway", "SW Morrison St", "SE Powell Blvd", "NE Glisan St", "NW Thurman St", "SE Clinton St", "NE Skidmore St"] },
    { city: "Eugene", zips: ["97401", "97402", "97403", "97404", "97405"],
      streets: ["Willamette St", "E 13th Ave", "W 7th Ave", "Coburg Rd", "Franklin Blvd", "Hilyard St", "Chambers St", "Barger Dr"] },
    { city: "Salem", zips: ["97301", "97302", "97303", "97304", "97305"],
      streets: ["Commercial St SE", "State St", "Center St NE", "Lancaster Dr SE", "Market St NE", "Hawthorne Ave SE", "Liberty St S"] },
    { city: "Bend", zips: ["97701", "97702", "97703"],
      streets: ["NW Wall St", "SW Century Dr", "NE 3rd St", "SW Highland Ave", "NW Franklin Ave", "SE Reed Market Rd"] },
    { city: "Beaverton", zips: ["97005", "97006", "97007", "97008"],
      streets: ["SW Broadway St", "SW Canyon Rd", "SW Hall Blvd", "SW Allen Blvd", "SW Watson Ave", "SW Farmington Rd"] },
    { city: "Corvallis", zips: ["97330", "97331", "97333"],
      streets: ["NW Monroe Ave", "SW 3rd St", "NW Van Buren Ave", "SW Philomath Blvd", "NW Harrison Blvd"] },
  ],
  MT: [
    { city: "Billings", zips: ["59101", "59102", "59105", "59106"],
      streets: ["Grand Ave", "Central Ave", "Poly Dr", "Rimrock Rd", "24th St W", "Monad Rd", "Broadwater Ave", "King Ave W"] },
    { city: "Missoula", zips: ["59801", "59802", "59803", "59808"],
      streets: ["Higgins Ave", "Brooks St", "Reserve St", "South Ave", "Stephens Ave", "Ryman St", "S 3rd St W"] },
    { city: "Bozeman", zips: ["59715", "59718"],
      streets: ["W Main St", "N 19th Ave", "W Babcock St", "S 11th Ave", "W Olive St", "Ferguson Ave"] },
    { city: "Helena", zips: ["59601", "59602"],
      streets: ["N Last Chance Gulch", "Euclid Ave", "Prospect Ave", "Montana Ave", "N Benton Ave", "Saddle Dr"] },
    { city: "Great Falls", zips: ["59401", "59404", "59405"],
      streets: ["Central Ave", "10th Ave S", "3rd St NW", "Fox Farm Rd", "2nd Ave N", "Smelter Ave NE"] },
    { city: "Kalispell", zips: ["59901"],
      streets: ["S Main St", "E Idaho St", "N Meridian Rd", "US Highway 93 S", "W Reserve Dr"] },
  ],
  DE: [
    { city: "Wilmington", zips: ["19801", "19803", "19805", "19806", "19808", "19809", "19810"],
      streets: ["N Market St", "Delaware Ave", "Concord Pike", "Kirkwood Hwy", "Lancaster Ave", "Foulk Rd", "N Union St", "Pennsylvania Ave", "W 4th St"] },
    { city: "Dover", zips: ["19901", "19904"],
      streets: ["S State St", "N DuPont Hwy", "Loockerman St", "S Governors Ave", "W Division St", "Hazel Rd"] },
    { city: "Newark", zips: ["19711", "19713"],
      streets: ["E Main St", "Elkton Rd", "S College Ave", "Cleveland Ave", "New London Rd", "Paper Mill Rd"] },
    { city: "Middletown", zips: ["19709"],
      streets: ["N Broad St", "W Main St", "E Lake St", "Levels Rd", "W Commerce St"] },
    { city: "Smyrna", zips: ["19977"],
      streets: ["S Dupont Blvd", "E Glenwood Ave", "N Main St", "Wheatleys Pond Rd"] },
    { city: "Milford", zips: ["19963"],
      streets: ["N Walnut St", "SE Front St", "Milford Harrington Hwy", "S Dupont Blvd", "Causey Ave"] },
  ],
  NH: [
    { city: "Manchester", zips: ["03101", "03102", "03103", "03104", "03109"],
      streets: ["Elm St", "S Willow St", "Hanover St", "Mammoth Rd", "Beech St", "Second St", "Union St", "Bridge St", "Chestnut St"] },
    { city: "Nashua", zips: ["03060", "03062", "03063", "03064"],
      streets: ["Main St", "Amherst St", "Daniel Webster Hwy", "E Dunstable Rd", "Broad St", "Concord St", "Exit 5 Dr"] },
    { city: "Concord", zips: ["03301", "03303"],
      streets: ["N Main St", "Loudon Rd", "S Main St", "Pleasant St", "Stickney Ave", "Hall St"] },
    { city: "Portsmouth", zips: ["03801"],
      streets: ["Congress St", "Islington St", "Lafayette Rd", "Woodbury Ave", "Maplewood Ave", "Middle St"] },
    { city: "Dover", zips: ["03820"],
      streets: ["Central Ave", "Locust St", "Silver St", "Washington St", "Cocheco St"] },
    { city: "Keene", zips: ["03431"],
      streets: ["Main St", "Winchester St", "West St", "Court St", "Emerald St"] },
    { city: "Salem", zips: ["03079"],
      streets: ["S Broadway", "N Broadway", "Main St", "Industrial Dr", "Cluff Crossing Rd"] },
  ],
  AK: [
    { city: "Anchorage", zips: ["99501", "99502", "99503", "99504", "99507", "99508", "99515", "99516", "99517", "99518"],
      streets: ["W 4th Ave", "E Northern Lights Blvd", "Spenard Rd", "Minnesota Dr", "Tudor Rd", "Abbott Rd", "Huffman Rd", "Lake Otis Pkwy", "C St", "Boniface Pkwy", "Muldoon Rd"] },
    { city: "Fairbanks", zips: ["99701", "99709", "99712"],
      streets: ["Airport Way", "College Rd", "Johansen Expy", "Cushman St", "Barnette St", "Peger Rd"] },
    { city: "Juneau", zips: ["99801"],
      streets: ["Egan Dr", "Glacier Hwy", "Mendenhall Loop Rd", "Douglas Hwy", "Channel Dr"] },
    { city: "Wasilla", zips: ["99654"],
      streets: ["E Parks Hwy", "N Main St", "S Knik Goose Bay Rd", "E Palmer Wasilla Hwy"] },
    { city: "Eagle River", zips: ["99577"],
      streets: ["Old Glenn Hwy", "E Eagle River Loop Rd", "Business Blvd", "Snowshoe Cir"] },
    { city: "Sitka", zips: ["99835"],
      streets: ["Lincoln St", "Harbor Dr", "Halibut Point Rd", "Sawmill Creek Rd"] },
  ],
};

export const TAX_FREE_STATES = Object.keys(PLACES) as readonly string[];

/** Human-readable label for the five statewide no-sales-tax states. */
export const TAX_FREE_STATE_LABEL = "AK / DE / MT / NH / OR";

function random(): number {
  return crypto.getRandomValues(new Uint32Array(1))[0] / 0x100000000;
}

function pick<T>(list: readonly T[], rng: () => number): T {
  return list[Math.floor(rng() * list.length)];
}

export function randomBillingExample(current: BillingInput, rng: () => number = random): BillingInput {
  const state = pick(TAX_FREE_STATES, rng);
  const place = pick(PLACES[state], rng);
  const street = pick(place.streets, rng);
  const zip = pick(place.zips, rng);
  const number = 100 + Math.floor(rng() * 8900);
  const unit = rng() < 0.25 ? ` Apt ${1 + Math.floor(rng() * 40)}` : "";
  return {
    ...current,
    country: "US",
    state,
    city: place.city,
    postal_code: zip,
    line1: `${number} ${street}${unit}`,
  };
}

/** Generated addresses are ready to submit; nothing else must be confirmed. */
export function billingExampleError(): string | null {
  return null;
}

/**
 * 固定的美国免税州账单地址（俄勒冈州波特兰，全州无销售税）。
 *
 * 为什么固定：Stripe 会在提交 tax_region 后按账单国家/州重算税额。
 * 地址每变一次，会话总额就变一次，而 confirm 的 expected_amount 必须
 * 等于会话当前总额，否则返回 checkout_amount_mismatch。
 * 钉死在免税州可以让总额恒定，也让同一张卡的重复测试可比。
 */
export const FIXED_TAX_FREE_BILLING = {
  country: "US",
  state: "OR",
  city: "Portland",
  postal_code: "97211",
  line1: "1 Fixture St",
} as const;

/** 固定地址的一句说明，给界面用。 */
export const FIXED_TAX_FREE_BILLING_LABEL = "美国免税州固定地址：OR / Portland 97211";
