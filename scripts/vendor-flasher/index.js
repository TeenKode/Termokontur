export { ESPLoader, Transport } from "esptool-js";
import SparkMD5 from "spark-md5";
export const md5 = data => SparkMD5.hashBinary(data);
