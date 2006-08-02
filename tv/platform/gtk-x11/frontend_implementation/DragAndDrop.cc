#define MOZILLA_INTERNAL_API
#include "DragAndDrop.h"
#include "XPCOMUtil.h"
#include <nsICommandManager.h>
#include <gtkmozembed.h>
#include <gtkmozembed_internal.h>
#include <nsIDragService.h>
#include <nsIDragSession.h>
#include <nsIDOMEvent.h>
#include <nsIDOMWindow.h>
#include <nsILocalFile.h>
#include <nsIComponentRegistrar.h>
#include <nsIWebBrowser.h>
#include <nsString.h>
#include <nsIClipboardDragDropHooks.h>
#include <nsIDragSession.h>
#include <nsISupportsPrimitives.h>
#include <nsITransferable.h>
#include <nsISupportsArray.h>
#include <nsICollection.h>
#include <stdio.h>
#include <string.h>


nsresult getDragData(nsIDOMElement* element, nsISupportsArray *dragArray) {
    // Create a transferable
    nsresult rv;
    nsCOMPtr<nsITransferable> trans(do_CreateInstance(
                "@mozilla.org/widget/transferable;1", &rv));
    // Add the mime-type
    nsAutoString dragSourceTypeStr = NS_ConvertUTF8toUTF16(
            nsDependentCString("dragsourcetype"));
    nsAutoString dragType;
    rv = element->GetAttribute(dragSourceTypeStr, dragType);
    nsCAutoString mimeType = NS_ConvertUTF16toUTF8(dragType);
    mimeType.Insert("application/x-democracy-", 0);
    mimeType.Append("-drag");
    trans->AddDataFlavor(PromiseFlatCString(mimeType).get());
    if(NS_FAILED(rv)) return rv;
    // Add the data
    nsAutoString bogusString = NS_ConvertUTF8toUTF16(
            nsDependentCString("BOGUS DATA"));
    nsCOMPtr<nsISupportsString> bogusData(do_CreateInstance(
                "@mozilla.org/supports-string;1", &rv));
    rv = bogusData->SetData(bogusString);
    if(NS_FAILED(rv)) return rv;
    rv = trans->SetTransferData(PromiseFlatCString(mimeType).get(), bogusData,
            bogusString.Length() * 2);
    if(NS_FAILED(rv)) return rv;
    // Turn that transferable into an nsISupportsArray
    nsCOMPtr<nsISupports> transSupports(do_QueryInterface(trans, &rv));
    if(NS_FAILED(rv)) return rv;
    nsCOMPtr<nsICollection> dragCollection(do_QueryInterface(dragArray, &rv));
    if(NS_FAILED(rv)) return rv;
    rv = dragCollection->AppendElement(transSupports);
    return rv;
}

nsresult startDrag(nsISupportsArray* dragArray) {
    nsresult rv;
    // Get the drag service and make sure we're not already doing a drop
    nsCOMPtr<nsIDragService> dragService(do_GetService(
                "@mozilla.org/widget/dragservice;1", &rv));
    if(NS_FAILED(rv)) return rv;
    nsCOMPtr<nsIDragSession> dragSession;
    rv = dragService->GetCurrentSession(getter_AddRefs(dragSession));
    if(NS_FAILED(rv)) return rv;
    if(dragSession != nsnull) return NS_ERROR_FAILURE;
    rv = dragService->InvokeDragSession(NULL, dragArray, NULL, 
            nsIDragService::DRAGDROP_ACTION_COPY);
    return rv;
}

nsresult isDragTypeSupported(nsAString &dragType, PRBool *supported)
{
    nsresult rv;

    nsCAutoString dragMimeType = NS_ConvertUTF16toUTF8(dragType);
    dragMimeType.Insert("application/x-democracy-", 0);
    dragMimeType.Append("-drag");
    nsCOMPtr<nsIDragService> dragService(do_GetService(
                "@mozilla.org/widget/dragservice;1", &rv));
    if(NS_FAILED(rv)) return rv;
    nsCOMPtr<nsIDragSession> dragSession;
    rv = dragService->GetCurrentSession(getter_AddRefs(dragSession));
    if(NS_FAILED(rv)) return rv;
    rv = dragSession->IsDataFlavorSupported(
            PromiseFlatCString(dragMimeType).get(), supported);
    return rv;
}


nsresult addDragHighlightCSSClass(nsIDOMElement* element) {
    nsAutoString classStr = NS_ConvertUTF8toUTF16(nsDependentCString("class"));
    nsAutoString cssClass;
    nsresult rv;
    rv = element->GetAttribute(classStr, cssClass);
    if(NS_FAILED(rv)) return rv;
    nsAString::const_iterator start, end;

    cssClass.BeginReading(start);
    cssClass.EndReading(end);
    nsAutoString dragHighlightStr = NS_ConvertUTF8toUTF16(
                    nsDependentCString("drag-highlight"));
    if (!FindInReadable(dragHighlightStr, start, end)) {
        cssClass.Append(NS_ConvertUTF8toUTF16(
                    nsDependentCString(" ")));
        cssClass.Append(dragHighlightStr);
        rv = element->SetAttribute(classStr, cssClass);
        if(NS_FAILED(rv)) return rv;
    }
    return NS_OK;
}

nsresult removeDragHighlightCSSClass(nsIDOMElement* element) {
    nsAutoString classStr = NS_ConvertUTF8toUTF16(nsDependentCString("class"));
    nsAutoString cssClass;
    nsresult rv;
    rv = element->GetAttribute(classStr, cssClass);
    if(NS_FAILED(rv)) return rv;
    nsAutoString dragHighlightStr = NS_ConvertUTF8toUTF16(
                    nsDependentCString(" drag-highlight"));
    for(int i = 0; i <= cssClass.Length() - 15; i++) {
        if(Substring(cssClass, i, 15).Equals(dragHighlightStr)) {
            cssClass.Cut(i, 15);
            break;
        }
    }
    rv = element->SetAttribute(classStr, cssClass);
    return rv;
}

class DemocracyDNDHook : public nsIClipboardDragDropHooks {
protected:
    GtkMozEmbed* embed;
    nsCOMPtr<nsIDOMElement> highlightedElement;

    void removeOldHighlight() {
        if(this->highlightedElement) {
            removeDragHighlightCSSClass(highlightedElement);
        }
        this->highlightedElement = nsnull;
    }

    void setNewHighlight(nsIDOMElement *element) {
        addDragHighlightCSSClass(element);
        this->highlightedElement = element;
    }

public:   
    DemocracyDNDHook(GtkMozEmbed* embed) {
        this->embed = embed;
    }

    NS_DECL_ISUPPORTS 

    nsresult AllowDrop(nsIDOMEvent *event, nsIDragSession *session, 
                    PRBool* retval) {
        nsresult rv;
        PRBool supported;
        *retval = false;
        
        this->removeOldHighlight();

        nsCOMPtr<nsIDOMElement> element;
        nsAutoString dragDestTypeString = NS_ConvertUTF8toUTF16(
                nsDependentCString("dragdesttype"));
        rv = searchUpForElementWithAttribute(event,
                dragDestTypeString, getter_AddRefs(element));
        if(NS_FAILED(rv)) return rv;
        if(element) {
            nsAutoString dragDestType;
            rv = element->GetAttribute(dragDestTypeString, dragDestType);
            if(NS_FAILED(rv)) return rv;
            nsCAutoString dragDestMimeType =
                NS_ConvertUTF16toUTF8(dragDestType);
            dragDestMimeType.Insert("application/x-democracy-", 0);
            dragDestMimeType.Append("-drag");
            rv = session->IsDataFlavorSupported(PromiseFlatCString(
                            dragDestMimeType).get(), &supported);
            if(NS_FAILED(rv)) return rv;
            if(supported) {
                *retval = true;
                this->setNewHighlight(element);
            }
        }
        return NS_OK;
    }

    nsresult AllowStartDrag(nsIDOMEvent *event, PRBool* retval) {
        *retval = true;

        nsCOMPtr<nsIDOMElement> element;
        nsAutoString dragSourceTypeStr = NS_ConvertUTF8toUTF16(
                nsDependentCString("dragsourcetype"));
        nsresult rv = searchUpForElementWithAttribute(event, 
                dragSourceTypeStr, getter_AddRefs(element));
        if (NS_FAILED(rv)) return rv;
        if(element) {
            nsCOMPtr<nsISupportsArray> dragArray(do_CreateInstance(
                        "@mozilla.org/supports-array;1", &rv));
            if (NS_FAILED(rv)) return rv;
            getDragData(element, dragArray);
            rv = startDrag(dragArray);
            if (NS_FAILED(rv)) {
                printf("WARNING: startDrag failed\n");
                return rv;
            } else {
                event->StopPropagation();
                event->PreventDefault();
                *retval = false;
            }
        } 
        return NS_OK;
    }

    nsresult OnCopyOrDrag(nsIDOMEvent *event, nsITransferable *trans, 
                    PRBool* retval) {
        /* This gets called when the default drop handler needs to copy
         * something.  Don't mess with things in this case.
         */
        *retval = true;
        return NS_OK;
    }

    nsresult OnPasteOrDrop(nsIDOMEvent *event, nsITransferable *trans, 
                    PRBool* retval) {
        this->removeOldHighlight();
        *retval = false;
        nsresult rv;
        nsCOMPtr<nsIDOMElement> element;
        nsAutoString dragDestTypeString = NS_ConvertUTF8toUTF16(
                nsDependentCString("dragdesttype"));
        nsAutoString dragDestDataString = NS_ConvertUTF8toUTF16(
                nsDependentCString("dragdestdata"));
        rv = searchUpForElementWithAttribute(event,
                dragDestTypeString, getter_AddRefs(element));
        if(NS_FAILED(rv)) return rv;
        if(element) {
            nsAutoString dragDestType;
            rv = element->GetAttribute(dragDestTypeString, dragDestType);
            if(NS_FAILED(rv)) return rv;
            nsAutoString dragDestData;
            rv = element->GetAttribute(dragDestDataString, dragDestData);
            if(NS_FAILED(rv)) return rv;
            PRBool supported;
            rv = isDragTypeSupported(dragDestType, &supported);
            if(supported) {
                *retval = true;
                nsCAutoString url = NS_ConvertUTF16toUTF8(dragDestData);
                url.Insert("action:handleDrop?data=", 0);
                gtk_moz_embed_load_url(this->embed,
                        PromiseFlatCString(url).get());
            } 
            return rv;
        } else {
            return NS_OK;
        }
    }
};

NS_IMPL_ISUPPORTS1(DemocracyDNDHook, nsIClipboardDragDropHooks) 

nsresult setupDragAndDrop(GtkMozEmbed* gtkembed)
{
    nsresult rv;

    nsCOMPtr<nsIWebBrowser> browser;
    nsCOMPtr<nsIDOMWindow> domWindow;

    gtk_moz_embed_get_nsIWebBrowser(gtkembed, getter_AddRefs(browser));
    rv = browser->GetContentDOMWindow(getter_AddRefs(domWindow));
    if (NS_FAILED(rv)) return rv;

    nsCOMPtr<nsICommandManager> commandManager(do_GetService(
            "@mozilla.org/embedcomp/command-manager;1", &rv));

    if (NS_FAILED(rv)) return rv;

    nsIClipboardDragDropHooks *rawPtr = new DemocracyDNDHook(gtkembed);
    if (!rawPtr)
        return NS_ERROR_OUT_OF_MEMORY;
    nsCOMPtr<nsIClipboardDragDropHooks> democracyDNDHook(do_QueryInterface(
                rawPtr, &rv));
    if (NS_FAILED(rv)) return rv;
    nsCOMPtr<nsICommandParams> params(do_CreateInstance(
        "@mozilla.org/embedcomp/command-params;1", &rv));
    if (NS_FAILED(rv)) return rv;
    rv = params->SetISupportsValue("addhook", democracyDNDHook);
    if (NS_FAILED(rv)) return rv;
    rv = commandManager->DoCommand("cmd_clipboardDragDropHook", params,
            domWindow);
    return rv;
}

