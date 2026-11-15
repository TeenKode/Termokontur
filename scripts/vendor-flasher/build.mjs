import {build} from 'esbuild';
import {readFileSync,writeFileSync} from 'node:fs';
await build({entryPoints:['index.js'],bundle:true,format:'esm',platform:'browser',target:'es2020',minify:true,legalComments:'eof',outfile:'../../termokontur/static/vendor/esptool.bundle.js'});
let licenses='Locally bundled dependencies of the browser flasher.\n\n';
for(const [name,file] of [['esptool-js','LICENSE'],['spark-md5','LICENSE'],['pako','LICENSE'],['atob-lite','LICENSE.md'],['tslib','LICENSE.txt']]){
 const pkg=JSON.parse(readFileSync('node_modules/'+name+'/package.json'));
 licenses+='\n=== '+name+' '+pkg.version+' ===\n'+readFileSync('node_modules/'+name+'/'+file,'utf8').replace(/\r\n/g,'\n')+'\n';
}
writeFileSync('../../termokontur/static/vendor/FLASHER-LICENSES.txt',licenses);
