(this.webpackJsonpdashboard=this.webpackJsonpdashboard||[]).push([[81],{406:function(e,n,t){"use strict";function a(e){!function(e){function n(e,n){return"___"+e.toUpperCase()+n+"___"}Object.defineProperties(e.languages["markup-templating"]={},{buildPlaceholders:{value:function(t,a,o,r){if(t.language===a){var i=t.tokenStack=[];t.code=t.code.replace(o,(function(e){if("function"===typeof r&&!r(e))return e;for(var o,s=i.length;-1!==t.code.indexOf(o=n(a,s));)++s;return i[s]=e,o})),t.grammar=e.languages.markup}}},tokenizePlaceholders:{value:function(t,a){if(t.language===a&&t.tokenStack){t.grammar=e.languages[a];var o=0,r=Object.keys(t.tokenStack);!function i(s){for(var c=0;c<s.length&&!(o>=r.length);c++){var u=s[c];if("string"===typeof u||u.content&&"string"===typeof u.content){var p=r[o],g=t.tokenStack[p],l="string"===typeof u?u:u.content,f=n(a,p),k=l.indexOf(f);if(k>-1){++o;var d=l.substring(0,k),h=new e.Token(a,e.tokenize(g,t.grammar),"language-"+a,g),m=l.substring(k+f.length),y=[];d&&y.push.apply(y,i([d])),y.push(h),m&&y.push.apply(y,i([m])),"string"===typeof u?s.splice.apply(s,[c,1].concat(y)):u.content=y}}else u.content&&i(u.content)}return s}(t.tokens)}}}})}(e)}e.exports=a,a.displayName="markupTemplating",a.aliases=[]}}]);